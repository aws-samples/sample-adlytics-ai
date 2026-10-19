# SPDX-License-Identifier: MIT-0
# ruff: noqa: PLR2004
"""One direct typed Bedrock Converse call per structured campaign-insight slot."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic
from typing import TYPE_CHECKING, Any, cast

import boto3
from botocore.config import Config
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectTimeoutError,
    ReadTimeoutError,
)

from adapters.outbound.aws_errors import merged_aws_failure_context
from adapters.outbound.telemetry.span_context import correlation
from adapters.outbound.telemetry.tracing import (
    INSIGHT_GENERATION,
    span,
    trace_boundary,
)
from domain.presentation.insights import (
    MAX_NEXT_STEP_REASON_CHARS,
    InsightDeepAnalysis,
    InsightGeneratedNarration,
    InsightGenerationRequest,
    InsightModelRoute,
    InsightNextStep,
    InsightOutputClass,
    InsightPromptTier,
    canonical_input_json,
    insight_input_document,
    resolve_next_step,
)
from domain.shared_kernel.errors import DependencyError, ErrorCode, InvariantError
from ports.outbound.insights import InsightGenerationPort

if TYPE_CHECKING:  # pragma: no cover
    from types_boto3_bedrock_runtime.client import BedrockRuntimeClient
    from types_boto3_bedrock_runtime.type_defs import ConverseResponseTypeDef

_INSIGHT_GENERATION_UNAVAILABLE = ErrorCode("INSIGHT", 4)
_INSIGHT_GENERATION_MALFORMED = ErrorCode("INSIGHT", 5)
_INSIGHT_GENERATION_TRUNCATED = ErrorCode("INSIGHT", 12)
# One tool per schema, pinned by toolChoice (ADR-076 decision 3): the target model
# family refuses the native structured-output parameter, and forced tool use is the
# verified portable envelope. The tool name matches the declared schema id; the deep
# tier arrives with its own schema, budget, and tool (slice G).
CARD_GENERATION_TOOL = "insight_narration_v1"
DEEP_GENERATION_TOOL = "insight_deep_dive_v1"
# Two governed sentences can exceed 256 tokens under the context-depth inputs; a
# truncated body is a typed failure, so the budget is sized per tier rather than hoped.
CARD_GENERATION_MAX_TOKENS = 512
# The measured bounded-analysis envelope (validation/RESULTS.md 2026-09-24): every
# dialog-shaped Opus 5 run completed tool_use within this budget, while an unbounded
# shape ran to the ceiling and truncated.
DEEP_GENERATION_MAX_TOKENS = 2_500
CARD_GENERATION_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["output_class", "sentences"],
    "properties": {
        "output_class": {"type": "string"},
        "sentences": {"type": "array", "items": {"type": "string"}},
    },
}
DEEP_GENERATION_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "what_stands_out",
        "candidate_readings",
        "discriminating_evidence",
        "cited_figures",
    ],
    "properties": {
        "what_stands_out": {"type": "string"},
        "candidate_readings": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 3,
            "description": (
                "Up to three candidate readings, each explicitly labeled as a "
                "reading, never stated as a finding."
            ),
        },
        "discriminating_evidence": {"type": "string"},
        # Kept in the deep shape because retained deep cache rows and the served
        # dialog response carry it (ADR-082); nothing refuses an analysis over it.
        "cited_figures": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "The findings[].value strings this analysis relies on, copied "
                "verbatim; an empty list is valid."
            ),
        },
    },
}


# The deep model's adaptive-thinking dial (product decision 2026-09-25: cap the deep
# tier at medium so first opens render faster). Validated live against the dev
# inference profile: the native field is `output_config.effort` (a bogus field and a
# bogus value are both refused, so acceptance means honored), default-effort deep
# generations measured 15-37s with one max_tokens truncation, medium 13.6-16.3s with
# none, low 8.8-10s. The card tier keeps the model default: Sonnet card generations
# run 2-3s and their calibrated behavior is frozen until the slice H cutover.
DEEP_GENERATION_EFFORT = "medium"


@dataclass(frozen=True, slots=True)
class _TierEnvelope:
    """One tier's forced-tool-use envelope: tool, schema, budget, and payload schema id."""

    tool_name: str
    schema: dict[str, object]
    max_tokens: int
    schema_id: str
    effort: str | None = None


_CARD_ENVELOPE = _TierEnvelope(
    CARD_GENERATION_TOOL,
    CARD_GENERATION_SCHEMA,
    CARD_GENERATION_MAX_TOKENS,
    "insight-narration-v1",
)
CARD_NEXT_STEP_SCHEMA_ID = "insight-narration-v2"
"""The card payload's schema id when the request serves next-step options (FR-11)."""


def card_envelope(request: InsightGenerationRequest) -> _TierEnvelope:
    """Return the card envelope; ``next_step`` is offered only when options exist (4.12).

    With no options the envelope is byte-identical to the dormant card shape, so
    ``allowed_kinds: []`` builds no schema property and changes no payload.
    """
    options = request.next_step_options
    if not options:
        return _CARD_ENVELOPE
    properties = cast("dict[str, object]", CARD_GENERATION_SCHEMA["properties"])
    schema: dict[str, object] = {
        **CARD_GENERATION_SCHEMA,
        "properties": {
            **properties,
            "next_step": {
                "type": "object",
                "additionalProperties": False,
                "required": ["step_id", "reason"],
                "properties": {
                    "step_id": {"type": "string", "enum": [item.step_id for item in options]},
                    "reason": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_NEXT_STEP_REASON_CHARS,
                    },
                },
            },
        },
    }
    return _TierEnvelope(
        CARD_GENERATION_TOOL, schema, CARD_GENERATION_MAX_TOKENS, CARD_NEXT_STEP_SCHEMA_ID
    )


_DEEP_ENVELOPE = _TierEnvelope(
    DEEP_GENERATION_TOOL,
    DEEP_GENERATION_SCHEMA,
    DEEP_GENERATION_MAX_TOKENS,
    "insight-deep-dive-v1",
    effort=DEEP_GENERATION_EFFORT,
)


@dataclass(frozen=True, slots=True)
class InsightModelRouteModels:
    """The configured model identifiers for the only routes a reviewed activation may select."""

    quick_model_id: str
    deep_model_id: str

    def __post_init__(self) -> None:
        if not self.quick_model_id.strip() or not self.deep_model_id.strip():
            raise ValueError(
                "every allowlisted insight route requires a configured model identifier."
            )

    @classmethod
    def shared(cls, model_id: str) -> InsightModelRouteModels:
        """Adapt the current single configured identifier until composition supplies both routes."""
        if not model_id.strip():
            raise ValueError("insight generation requires a configured model identifier.")
        return cls(model_id, model_id)

    def model_id_for(self, route: InsightModelRoute) -> str:
        """Resolve the model only from the validated route carried by an active activation."""
        return self.quick_model_id if route is InsightModelRoute.QUICK else self.deep_model_id


class BedrockInsightGenerator(InsightGenerationPort):
    """Direct forced-tool-use Converse generation with a bounded SDK transport."""

    def __init__(
        self,
        model_routes: InsightModelRouteModels | str,
        timeout_seconds: int,
        client: BedrockRuntimeClient | None = None,
    ) -> None:
        # Connect and read are independent transport failure boundaries. The calling Lambda/API
        # envelope may end first; no application-level insight deadline is implied here. The
        # ceiling admits the continuation worker's patient deep transport; API-fronted callers
        # stay bounded by their own deployed envelope, not this constructor.
        if not 1 <= timeout_seconds <= 90:
            raise ValueError("insight generation requires a 1-90 second timeout.")
        self._model_routes = (
            InsightModelRouteModels.shared(model_routes)
            if isinstance(model_routes, str)
            else model_routes
        )
        self._timeout_seconds = timeout_seconds
        self._client = client or boto3.client(
            "bedrock-runtime",
            config=Config(
                connect_timeout=timeout_seconds,
                read_timeout=timeout_seconds,
                retries={"max_attempts": 0},
            ),
        )

    def generate(self, request: InsightGenerationRequest) -> InsightGeneratedNarration:
        """Perform exactly one card-tier forced-tool-use Converse call."""
        if request.key.tier is not InsightPromptTier.CARD:
            raise ValueError("card generation requires a card-tier cache identity.")
        return cast(
            "InsightGeneratedNarration",
            _traced_insight_generation(
                _TracedGeneration(
                    self._converse,
                    request,
                    self._model_routes.model_id_for(request.prompt.model_route),
                    self._timeout_seconds,
                    card_envelope(request),
                    self._narration,
                )
            ),
        )

    def generate_deep(self, request: InsightGenerationRequest) -> InsightDeepAnalysis:
        """Perform exactly one deep-tier forced-tool-use Converse call (ADR-076 decision 1)."""
        if request.key.tier is not InsightPromptTier.DEEP:
            raise ValueError("deep generation requires a deep-tier cache identity.")
        return cast(
            "InsightDeepAnalysis",
            _traced_insight_generation(
                _TracedGeneration(
                    self._converse,
                    request,
                    self._model_routes.model_id_for(request.prompt.model_route),
                    self._timeout_seconds,
                    _DEEP_ENVELOPE,
                    self._deep_analysis,
                )
            ),
        )

    def _deep_analysis(
        self, response: ConverseResponseTypeDef, request: InsightGenerationRequest
    ) -> tuple[InsightDeepAnalysis, str]:
        self._require_completed(response, request)
        return self._decode_deep(response, request), "generated"

    def _converse(
        self, request: InsightGenerationRequest, envelope: _TierEnvelope
    ) -> ConverseResponseTypeDef:
        # The effort dial rides the native passthrough; snake_case is the accepted
        # form (camelCase is refused — validated live 2026-09-25).
        effort_fields = (
            {"additionalModelRequestFields": {"output_config": {"effort": envelope.effort}}}
            if envelope.effort is not None
            else {}
        )
        try:
            response = self._client.converse(
                modelId=self._model_routes.model_id_for(request.prompt.model_route),
                system=[{"text": request.prompt.prompt}],
                messages=[
                    {
                        "role": "user",
                        "content": [{"text": self._input_json(request, envelope.schema_id)}],
                    }
                ],
                # No sampling parameters: serve-stability comes from content-identity
                # caching (each identity generates once), and the target model family
                # refuses `temperature` as deprecated (ADR-076 decision 3).
                inferenceConfig={"maxTokens": envelope.max_tokens},
                toolConfig={
                    "tools": [
                        {
                            "toolSpec": {
                                "name": envelope.tool_name,
                                "description": "Deliver the structured insight output.",
                                "inputSchema": {"json": cast("Any", envelope.schema)},
                            }
                        }
                    ],
                    "toolChoice": {"tool": {"name": envelope.tool_name}},
                },
                **cast("Any", effort_fields),
            )
        except ClientError as error:
            context = merged_aws_failure_context(
                error, {"cause": type(error).__name__, "slot": request.key.slot_id.value}
            )
            if context.get("service_code") == "ValidationException":
                raise InvariantError(
                    _INSIGHT_GENERATION_MALFORMED,
                    "Insight generation request failed provider validation.",
                    context=context,
                ) from error
            raise DependencyError(
                _INSIGHT_GENERATION_UNAVAILABLE,
                "Insight narration could not be generated.",
                context=context,
            ) from error
        except BotoCoreError as error:
            raise DependencyError(
                _INSIGHT_GENERATION_UNAVAILABLE,
                "Insight narration could not be generated.",
                context=merged_aws_failure_context(
                    error, {"cause": type(error).__name__, "slot": request.key.slot_id.value}
                ),
            ) from error
        return response

    def _narration(
        self, response: ConverseResponseTypeDef, request: InsightGenerationRequest
    ) -> tuple[InsightGeneratedNarration, str]:
        self._require_completed(response, request)
        narration = self._decode(response, request)
        # Structural only (ADR-082 §3): the declared output class must match the
        # slot's. What the sentences say, figures included, is governed by the
        # prompt and measured by calibration and evaluation, never parsed here.
        if narration.output_class is not request.output_class:
            raise InvariantError(
                _INSIGHT_GENERATION_MALFORMED,
                "Insight output class differs from its declaration.",
                context={
                    "slot": request.key.slot_id.value,
                    "violation": "output class differs from the declared slot class",
                },
            )
        return narration, ("silent" if narration.text is None else "generated")

    @staticmethod
    def _input_json(request: InsightGenerationRequest, schema_id: str) -> str:
        return model_input_json(request, schema_id)

    @staticmethod
    def _require_completed(
        response: ConverseResponseTypeDef, request: InsightGenerationRequest
    ) -> None:
        """Type an incomplete generation before its truncated body decodes as malformed.

        A body cut by the output-token budget is a named, diagnosable failure class,
        never a generic malformed-output invariant: the stop reason and token usage
        are the exact diagnostics a log-only RCA needs (never raw model text). A
        forced tool call ends with the ``tool_use`` stop reason; anything else means
        the pinned tool never completed its input.
        """
        wire = cast("dict[str, object]", response)
        stop_reason = wire.get("stopReason")
        if not isinstance(stop_reason, str) or stop_reason == "tool_use":
            return
        usage = wire.get("usage")
        usage_context = (
            {
                name: value
                for name, value in cast("dict[str, object]", usage).items()
                if isinstance(value, int)
            }
            if isinstance(usage, dict)
            else {}
        )
        raise InvariantError(
            _INSIGHT_GENERATION_TRUNCATED,
            "Insight generation stopped before completing its structured output.",
            context={
                "slot": request.key.slot_id.value,
                "stop_reason": stop_reason,
                "usage": usage_context,
            },
        )

    @staticmethod
    def _tool_input(response: ConverseResponseTypeDef, tool_name: str) -> dict[str, object]:
        """Read the pinned tool's already-parsed input from exactly one toolUse block.

        A stray text block cannot corrupt the pinned tool's structured input, so text
        blocks are ignored; a second toolUse block is ambiguity and is refused.
        """
        wire = cast("dict[str, object]", response)
        output_value = wire.get("output")
        if not isinstance(output_value, dict):
            raise ValueError("structured response has no output.")
        message_value = cast("dict[str, object]", output_value).get("message")
        if not isinstance(message_value, dict):
            raise ValueError("structured response has no message.")
        content_value = cast("dict[str, object]", message_value).get("content")
        if not isinstance(content_value, list):
            raise ValueError("structured response has no content list.")
        tool_uses: list[dict[str, object]] = []
        for raw in cast("list[object]", content_value):
            if isinstance(raw, dict):
                tool_use = cast("dict[str, object]", raw).get("toolUse")
                if isinstance(tool_use, dict):
                    tool_uses.append(cast("dict[str, object]", tool_use))
        if len(tool_uses) != 1:
            raise ValueError("structured response must contain exactly one tool call.")
        if tool_uses[0].get("name") != tool_name:
            raise ValueError("structured response tool call is not the pinned tool.")
        tool_input = tool_uses[0].get("input")
        if not isinstance(tool_input, dict):
            raise ValueError("structured response tool input is not a mapping.")
        return cast("dict[str, object]", tool_input)

    @staticmethod
    def _decode(
        response: ConverseResponseTypeDef, request: InsightGenerationRequest
    ) -> InsightGeneratedNarration:
        try:
            payload = BedrockInsightGenerator._tool_input(response, CARD_GENERATION_TOOL)
            fields = set(payload)
            # next_step is optional and exists only when this request served options.
            if fields - {"next_step"} != {"output_class", "sentences"} or (
                "next_step" in fields and not request.next_step_options
            ):
                raise ValueError("structured response fields are not exact.")
            output_class, sentences = payload["output_class"], payload["sentences"]
            if not isinstance(output_class, str) or not isinstance(sentences, list):
                raise ValueError("structured response fields are malformed.")
            sentence_values = cast("list[object]", sentences)
            if not all(isinstance(value, str) for value in sentence_values):
                raise ValueError("structured response arrays must contain text.")
            if output_class not in {item.value for item in InsightOutputClass}:
                # Named before enum construction so the violation summary stays a
                # bounded adapter message rather than echoing a model-authored token.
                raise ValueError("structured response output class is undeclared.")
            return InsightGeneratedNarration(
                InsightOutputClass(output_class),
                tuple(cast("list[str]", sentence_values)),
                _next_step(payload.get("next_step"), request),
            )
        except (KeyError, TypeError, ValueError) as error:
            # The violation summary carries only this adapter's bounded validation
            # message — never model text. The provider parses the tool input, so a
            # JSON decode failure cannot reach this seam.
            raise InvariantError(
                _INSIGHT_GENERATION_MALFORMED,
                "Insight generation returned malformed structured output.",
                context={
                    "slot": request.key.slot_id.value,
                    "violation": f"{type(error).__name__}: {error}",
                },
            ) from error

    @staticmethod
    def _decode_deep(
        response: ConverseResponseTypeDef, request: InsightGenerationRequest
    ) -> InsightDeepAnalysis:
        try:
            payload = BedrockInsightGenerator._tool_input(response, DEEP_GENERATION_TOOL)
            if set(payload) != {
                "what_stands_out",
                "candidate_readings",
                "discriminating_evidence",
                "cited_figures",
            }:
                raise ValueError("structured response fields are not exact.")
            standout = payload["what_stands_out"]
            readings = payload["candidate_readings"]
            evidence = payload["discriminating_evidence"]
            cited = payload["cited_figures"]
            if (
                not isinstance(standout, str)
                or not isinstance(evidence, str)
                or not isinstance(readings, list)
                or not isinstance(cited, list)
            ):
                raise ValueError("structured response fields are malformed.")
            reading_values = cast("list[object]", readings)
            cited_values = cast("list[object]", cited)
            if not all(isinstance(value, str) for value in [*reading_values, *cited_values]):
                raise ValueError("structured response arrays must contain text.")
            # Provider-side tool-input validation covers array/string types but not
            # uniqueness or nonblank strings; structural noise is canonicalized so
            # the domain shape invariant holds. No value is invented.
            canonical_citations = tuple(
                dict.fromkeys(
                    value.strip() for value in cast("list[str]", cited_values) if value.strip()
                )
            )
            return InsightDeepAnalysis(
                standout,
                tuple(cast("list[str]", reading_values)),
                evidence,
                canonical_citations,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise InvariantError(
                _INSIGHT_GENERATION_MALFORMED,
                "Deep insight generation returned malformed structured output.",
                context={
                    "slot": request.key.slot_id.value,
                    "violation": f"{type(error).__name__}: {error}",
                },
            ) from error


def _next_step(value: object, request: InsightGenerationRequest) -> InsightNextStep | None:
    """Resolve the chosen step by id; structural checks only (ADR-082, design 4.12)."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("structured next step is malformed.")
    step = cast("dict[str, object]", value)
    if set(step) != {"step_id", "reason"}:
        raise ValueError("structured next step fields are not exact.")
    step_id, reason = step["step_id"], step["reason"]
    if not isinstance(step_id, str) or not isinstance(reason, str):
        raise ValueError("structured next step fields are malformed.")
    return resolve_next_step(request.next_step_options, step_id, reason)


def model_input_json(request: InsightGenerationRequest, schema_id: str) -> str:
    """Return the exact user-message JSON one generation sends to the model.

    The domain document is the one home of the input shape (and of the card key's input
    digest), so the payload and the cache identity cannot drift. Display context informs
    wording only; the figures a narration may state are the deterministic findings and what
    validly derives from them (ADR-068 context-depth amendment, ADR-082 §1).
    """
    return canonical_input_json(
        {
            **insight_input_document(request),
            "schema": schema_id,
            # The deep shape has no silent form: it runs only for a filled card whose
            # findings were already material (contract §3.1).
            "silence_allowed": request.key.tier is InsightPromptTier.CARD,
        }
    )


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class _TracedGeneration:
    """One tier's traced generation: the envelope plus its converse and decode seams."""

    converse: Callable[[InsightGenerationRequest, _TierEnvelope], ConverseResponseTypeDef]
    request: InsightGenerationRequest
    model_id: str
    timeout_seconds: int
    envelope: _TierEnvelope
    decode: Callable[[ConverseResponseTypeDef, InsightGenerationRequest], tuple[object, str]]


@trace_boundary(INSIGHT_GENERATION)
def _traced_insight_generation(traced: _TracedGeneration) -> object:
    """Run one forced-tool-use generation under the shared `insight.model.call` span.

    The single boundary owner for the span (trace-contract rule): both tiers route
    through here, so every LLM call — serve-time card, on-request deep, continuation,
    or precompute — reports its latency, token usage, stop reason, tier, and outcome
    under whichever handler invoked it. Those measurements are what UX decisions
    about refresh-free insight delivery are made from.
    """
    request = traced.request
    corr = correlation()
    with span(
        INSIGHT_GENERATION,
        slot_id=request.key.slot_id.value,
        model_route=request.prompt.model_route.value,
        model_id=traced.model_id,
        run_id=None if corr is None else corr.run_id,
        task_id=None if corr is None else corr.task_id,
        cohort_revision=None if corr is None else corr.cohort_revision,
        configured_timeout_ms=traced.timeout_seconds * 1_000,
        tier=request.key.tier.value,
        horizon_role=request.window_profile.role.value,
    ) as recording:
        started = monotonic()
        recording.record(
            prompt_version=request.key.prompt_version,
            output_class=request.output_class.value,
            # ADR-076 decision 1 (2026-10-05): which exact input and cache row this call
            # served, without prompt, narration or scope content.
            input_sha256=_sha256(model_input_json(request, traced.envelope.schema_id)),
            cache_identity_sha256=_sha256(
                f"{request.key.ownership_key}\n{request.key.content_key}"
            ),
        )
        try:
            response = traced.converse(request, traced.envelope)
        except Exception as error:
            recording.record(
                observed_timeout=isinstance(
                    error.__cause__, (ConnectTimeoutError, ReadTimeoutError)
                ),
                outcome="timeout"
                if isinstance(error.__cause__, (ConnectTimeoutError, ReadTimeoutError))
                else "failed",
            )
            raise
        recording.record(observed_timeout=False)
        usage = cast("dict[str, object]", response.get("usage") or {})
        input_usage, output_usage = usage.get("inputTokens"), usage.get("outputTokens")
        # Recorded before the truncation/decode checks can raise, so a failed
        # generation still carries its usage; `record` drops non-int None values.
        recording.record(
            stop_reason=str(response.get("stopReason", "")),
            usage_input=input_usage if isinstance(input_usage, int) else None,
            usage_output=output_usage if isinstance(output_usage, int) else None,
        )
        result, outcome = traced.decode(response, request)
        recording.record(
            outcome=outcome,
            total_millis=int((monotonic() - started) * 1_000),
        )
        return result
