# SPDX-License-Identifier: MIT-0
"""Bounded Bedrock outcome calibration of insight prompts against fictional payloads.

ADR-076 decision 8 (amended 2026-09-24): "passed" attests generated-output quality,
never summary classification. The known-correct and exploratory cases GENERATE through
the candidate prompt from the case's fictional payload in the production envelope
shape; every other family judges its supplied fictional draft — the must-reject
contradiction, fluent-unsupported, miscomputed-derivation, and incompatible-basis
drafts, and the must-accept derived-figure draft (ADR-082 §4). Only structural failures
are decided without the evaluator: a malformed, truncated, wrong-class, or silent
generation fails its must-accept case. Every narration's figures and wording are judged
by the calibrated evaluator (ADR-082: no text check precedes it), one case per call with
the calls in parallel, and each disposition must match its family's expected disposition.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from contextvars import Context, copy_context
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, cast

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

# The candidate prompt generates through the production tier envelope (one tool per
# schema, pinned toolChoice, ADR-076 decision 3), exactly as serve-time generation
# does: the tools, schemas, and token budgets are the generator's own constants, so
# calibration cannot drift from production.
from adapters.outbound.aws_errors import merged_aws_failure_context
from adapters.outbound.bedrock.insights import (
    CARD_GENERATION_MAX_TOKENS,
    CARD_GENERATION_SCHEMA,
    CARD_GENERATION_TOOL,
    DEEP_GENERATION_EFFORT,
    DEEP_GENERATION_MAX_TOKENS,
    DEEP_GENERATION_SCHEMA,
    DEEP_GENERATION_TOOL,
    InsightModelRouteModels,
)
from domain.presentation.insights import (
    InsightCalibrationCase,
    InsightCalibrationCaseResult,
    InsightCalibrationDisposition,
    InsightCalibrationKind,
    InsightCalibrationOutcome,
    InsightCalibrationResult,
    InsightDeepAnalysis,
    InsightPromptCalibrationRunRequest,
    InsightPromptTier,
    decimal_figure,
    insight_window_profile,
    preset_window_days,
)
from domain.shared_kernel.errors import DependencyError, ErrorCode, InvariantError
from domain.shared_kernel.values import DateRange
from ports.outbound.insights import InsightPromptCalibrationRunner

if TYPE_CHECKING:  # pragma: no cover
    from types_boto3_bedrock_runtime.client import BedrockRuntimeClient
    from types_boto3_bedrock_runtime.type_defs import ConverseResponseTypeDef

_CALIBRATION_UNAVAILABLE = ErrorCode("INSIGHT", 6)
_CALIBRATION_MALFORMED = ErrorCode("INSIGHT", 7)
_MAX_TIMEOUT_SECONDS = 90
_CONNECT_TIMEOUT_SECONDS = 5
_EVALUATOR_TIMEOUT_SECONDS = 10
# The evaluator judges each structurally valid output alone, and the calls run in
# parallel. Batched judging let one case's findings bleed into its neighbour's verdict
# (the 2026-09-28 probe). The budget covers the one-line check that precedes each
# disposition.
_EVALUATOR_TOOL = "insight_calibration_v1"
_EVALUATOR_MAX_TOKENS = 512
# One case per call needs no case identifier and no array. The flat shape also
# removes a failure the probe measured: the model sometimes JSON-encoded a nested
# array as a string.
_EVALUATOR_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["check", "disposition"],
    "properties": {
        # Working the judge writes before it decides. It is never persisted; only the
        # disposition is read.
        "check": {"type": "string"},
        "disposition": {"type": "string", "enum": ["accept", "reject"]},
    },
}
_EVALUATOR_INSTRUCTION = (
    "The case carries fictional supplied findings, the window they cover, and a "
    "candidate narration produced for them. Judge only the narration against its "
    "findings and window. Return accept when the "
    "narration is honest about what it is: observed statements match supplied values "
    "and directions; interpretations, hypotheses, and rounded restatements of supplied "
    "values are acceptable exactly when they are worded as readings rather than facts. "
    "A figure correctly derived from the supplied values (a difference, percentage "
    "change, share, comparison, or a projection labeled as one) is acceptable when it "
    "keeps the qualifiers of the values it came from. A conclusion that follows "
    "necessarily from the supplied values is derived too, not unsupported: a share "
    "above half is the largest part, and a positive change is a rise. "
    "Return reject when the narration contradicts a supplied value or direction; states "
    "a figure that is neither supplied nor correctly derived from supplied values; "
    "miscomputes a derivation; combines values of incompatible basis, grain, window, "
    "or currency; states a cause, benchmark, or prediction the findings do not support "
    "as if it were fact; or merely restates the supplied values without any reading. "
    "Do not penalize interpretation, hedged hypotheses, or rounded figures in prose. "
    "First write check: one short line recomputing every figure the "
    "narration states or derives from the supplied values, and noting any direction, "
    "basis, or support problem; then give the disposition that follows from it."
)


# A fictional served window start. Production profiles carry the served artifact's
# aligned prior period, and the cases' findings name it ("prior window"), so the
# calibration profile declares one too; a null prior told the judge none existed.
_FICTIONAL_WINDOW_START = date(2031, 3, 15)


def _window_profile(case: InsightCalibrationCase) -> dict[str, object]:
    """Render the case's window through the domain builder production uses, for both calls."""
    days = preset_window_days(case.horizon)
    window = DateRange(_FICTIONAL_WINDOW_START, _FICTIONAL_WINDOW_START + timedelta(days=days - 1))
    prior_end = _FICTIONAL_WINDOW_START - timedelta(days=1)
    prior = DateRange(prior_end - timedelta(days=days - 1), prior_end)
    profile = insight_window_profile(window, prior)
    if profile.role is not case.horizon:
        raise ValueError("calibration window length does not declare the case's horizon.")
    return {
        "days": profile.days,
        "grain": profile.grain.value,
        "prior_start": profile.prior_start,
        "prior_end": profile.prior_end,
        "role": profile.role.value,
    }


@dataclass(frozen=True, slots=True)
class _CalibrationCall:
    """One forced-tool-use Converse invocation shape: a generation or the judgment."""

    system: str
    body: str
    tool_name: str
    schema: dict[str, object]
    max_tokens: int
    effort: str | None = None


@dataclass(frozen=True, slots=True)
class _CandidateOutput:
    """Tier-neutral calibration output reduced to the evaluator input."""

    sentences: tuple[str, ...]

    @property
    def text(self) -> str | None:
        return None if not self.sentences else " ".join(self.sentences)


@dataclass(frozen=True, slots=True)
class _JudgedCase:
    """One case whose structurally valid narration awaits the evaluator."""

    case: InsightCalibrationCase
    sentences: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BedrockInsightCalibrationRunner(InsightPromptCalibrationRunner):
    """Generate and judge one closed, bounded batch of fictional cases per prompt version."""

    model_routes: InsightModelRouteModels
    timeout_seconds: int
    client: BedrockRuntimeClient | None = None
    evaluator_client: BedrockRuntimeClient | None = None

    def __post_init__(self) -> None:
        if not 1 <= self.timeout_seconds <= _MAX_TIMEOUT_SECONDS:
            raise ValueError("insight calibration requires a 1-90 second generation timeout.")
        injected_client = self.client is not None
        if self.client is None:
            object.__setattr__(
                self,
                "client",
                self._client(self.timeout_seconds),
            )
        if self.evaluator_client is None:
            object.__setattr__(
                self,
                "evaluator_client",
                # Unit/integration injection supplies one controlled client for both
                # call types. Runtime composition gets a separate patient evaluator
                # client. The worker is outside the HTTP deadline, so a slow valid check
                # should finish rather than grind a full-run queue retry.
                self.client if injected_client else self._client(_EVALUATOR_TIMEOUT_SECONDS),
            )

    @staticmethod
    def _client(timeout_seconds: int) -> BedrockRuntimeClient:
        return boto3.client(
            "bedrock-runtime",
            config=Config(
                connect_timeout=min(_CONNECT_TIMEOUT_SECONDS, timeout_seconds),
                read_timeout=timeout_seconds,
                retries={"max_attempts": 0},
            ),
        )

    def run(self, request: InsightPromptCalibrationRunRequest) -> InsightCalibrationResult:
        """Generate the prompt-generated cases, then judge every structurally valid one."""
        outcomes: dict[InsightCalibrationKind, InsightCalibrationOutcome] = {}
        judged: list[_JudgedCase] = []
        generated: dict[InsightCalibrationKind, _CandidateOutput | None] = {}
        must_generate = tuple(case for case in request.cases if case.draft is None)

        # The two generated cases are independent for either tier. One bounded
        # two-worker wave keeps their p95 latency from adding serially before the
        # evaluator, so the worst case stays one generation-timeout plus one evaluator
        # timeout — fail-closed inside the 25s Lambda with terminal-write headroom.
        def _one(job: tuple[Context, InsightCalibrationCase]) -> _CandidateOutput | None:
            context, case = job
            return context.run(self._generated, request, case)

        generation_jobs = tuple((copy_context(), case) for case in must_generate)
        with ThreadPoolExecutor(max_workers=len(must_generate)) as executor:
            values = executor.map(_one, generation_jobs)
            generated.update(
                (case.kind, value) for case, value in zip(must_generate, values, strict=True)
            )
        for case in request.cases:
            candidate = (
                generated.get(case.kind)
                if case.draft is None
                else _CandidateOutput(case.draft.sentences)
            )
            structural = self._structural_outcome(case, candidate)
            if structural is not None:
                outcomes[case.kind] = structural
                continue
            judged.append(_JudgedCase(case, cast("_CandidateOutput", candidate).sentences))
        evaluator_id: str | None = None
        if judged:
            # The evaluator is a fixed judging role, not the candidate prompt's
            # delivery tier. Each case is judged alone, all in one parallel wave, so
            # the wave keeps inside Sonnet 5's separate 6s transport after the parallel
            # Opus generation wave; its exact identity is stamped on the result. Any
            # failed call fails the whole run, which then stays pending.
            evaluator_id = self.model_routes.quick_model_id

            def _judge(job: tuple[Context, _JudgedCase]) -> InsightCalibrationDisposition:
                context, item = job
                return context.run(self._judged_disposition, request, item)

            judge_jobs = tuple((copy_context(), item) for item in judged)
            with ThreadPoolExecutor(max_workers=len(judged)) as executor:
                dispositions = tuple(executor.map(_judge, judge_jobs))
            for item, disposition in zip(judged, dispositions, strict=True):
                outcomes[item.case.kind] = (
                    InsightCalibrationOutcome.PASSED
                    if disposition is item.case.expected_disposition
                    else InsightCalibrationOutcome.FAILED
                )
        return InsightCalibrationResult(
            tuple(
                InsightCalibrationCaseResult(case.kind, outcomes[case.kind])
                for case in request.cases
            ),
            evaluator=evaluator_id,
        )

    def _generated(
        self, request: InsightPromptCalibrationRunRequest, case: InsightCalibrationCase
    ) -> _CandidateOutput | None:
        """Generate through the candidate tier's production envelope; None means failure."""
        deep = request.tier is InsightPromptTier.DEEP
        call = _CalibrationCall(
            system=request.prompt,
            body=self._generation_payload(request, case),
            tool_name=DEEP_GENERATION_TOOL if deep else CARD_GENERATION_TOOL,
            schema=DEEP_GENERATION_SCHEMA if deep else CARD_GENERATION_SCHEMA,
            max_tokens=DEEP_GENERATION_MAX_TOKENS if deep else CARD_GENERATION_MAX_TOKENS,
            # Calibration attests the production envelope, so a deep candidate
            # generates under the same effort dial the serve path uses.
            effort=DEEP_GENERATION_EFFORT if deep else None,
        )
        response = self._converse(request, call)
        try:
            self._require_completed(response, request)
            payload = self._tool_input(response, call.tool_name)
            if deep:
                if set(payload) != {
                    "what_stands_out",
                    "candidate_readings",
                    "discriminating_evidence",
                    "cited_figures",
                }:
                    raise ValueError("structured deep response fields are not exact.")
                standout = payload["what_stands_out"]
                readings = payload["candidate_readings"]
                evidence = payload["discriminating_evidence"]
                if (
                    not isinstance(standout, str)
                    or not isinstance(evidence, str)
                    or not isinstance(readings, list)
                    or not all(isinstance(value, str) for value in cast("list[object]", readings))
                ):
                    raise ValueError("structured deep response fields are malformed.")
                deep_analysis = InsightDeepAnalysis(
                    standout,
                    tuple(cast("list[str]", readings)),
                    evidence,
                    tuple(
                        dict.fromkeys(
                            value.strip()
                            for value in cast("list[object]", payload["cited_figures"])
                            if isinstance(value, str) and value.strip()
                        )
                    ),
                )
                sentences = (
                    deep_analysis.what_stands_out,
                    *deep_analysis.candidate_readings,
                    deep_analysis.discriminating_evidence,
                )
            else:
                if set(payload) != {"output_class", "sentences"}:
                    raise ValueError("structured response fields are not exact.")
                if payload["output_class"] != request.output_class.value:
                    raise ValueError("generated output class differs from the declaration.")
                raw_sentences = payload["sentences"]
                if not isinstance(raw_sentences, list) or not all(
                    isinstance(value, str) for value in cast("list[object]", raw_sentences)
                ):
                    raise ValueError("structured response sentences are malformed.")
                sentences = tuple(cast("list[str]", raw_sentences))
            candidate = _CandidateOutput(tuple(sentences))
            if candidate.text is None:
                return candidate
            if any(not sentence.strip() for sentence in candidate.sentences):
                raise ValueError("structured response sentences must be nonblank.")
            return candidate
        # A malformed or truncated generation is itself the candidate prompt's
        # failure under this fictional payload; the case records it as FAILED for
        # a must-accept family rather than aborting the whole run.
        # error-propagation: a failed draft is the case's outcome, decided by _structural_outcome
        except InvariantError, KeyError, TypeError, ValueError:
            return None

    def _structural_outcome(
        self, case: InsightCalibrationCase, narration: _CandidateOutput | None
    ) -> InsightCalibrationOutcome | None:
        """Decide only the structurally failed cases; None sends the case to the evaluator."""
        if narration is None or narration.text is None:
            # Generation failed or went silent on a payload with material findings:
            # over-restriction fails a must-accept case; a must-reject case cannot be
            # decided by a missing draft (drafts always carry text).
            return (
                InsightCalibrationOutcome.FAILED
                if case.expected_disposition is InsightCalibrationDisposition.ACCEPT
                else InsightCalibrationOutcome.PASSED
            )
        return None

    def _judged_disposition(
        self, request: InsightPromptCalibrationRunRequest, item: _JudgedCase
    ) -> InsightCalibrationDisposition:
        # The judge sees the evidence the generator had (findings and window profile)
        # and the narration. The case's family and summary name the expected verdict,
        # and the evaluator is the only decider (ADR-082), so neither may reach it.
        body = json.dumps(
            {
                "schema": "insight-calibration-v4",
                "instruction": _EVALUATOR_INSTRUCTION,
                "window_profile": _window_profile(item.case),
                "findings": [
                    {"name": finding.name, "value": decimal_figure(finding.value)}
                    for finding in item.case.findings
                ],
                "narration": list(item.sentences),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        response = self._converse(
            request,
            _CalibrationCall(
                # The evaluator judges under its own fixed instruction, never under the
                # candidate prompt: the candidate is the subject, not the judge.
                system=_EVALUATOR_INSTRUCTION,
                body=body,
                tool_name=_EVALUATOR_TOOL,
                schema=_EVALUATOR_SCHEMA,
                max_tokens=_EVALUATOR_MAX_TOKENS,
            ),
        )
        self._require_completed(response, request)
        payload = self._tool_input(response, _EVALUATOR_TOOL)
        try:
            check = payload.get("check")
            if (
                set(payload) != {"check", "disposition"}
                or not isinstance(check, str)
                or not check.strip()
            ):
                raise ValueError("calibration evaluator fields are not exact.")
            return InsightCalibrationDisposition(payload["disposition"])
        except (KeyError, TypeError, ValueError) as error:
            raise InvariantError(
                _CALIBRATION_MALFORMED,
                "Insight calibration returned malformed structured output.",
            ) from error

    @staticmethod
    def _generation_payload(
        request: InsightPromptCalibrationRunRequest, case: InsightCalibrationCase
    ) -> str:
        """Render the case as the candidate tier's production generation payload shape."""
        deep = request.tier is InsightPromptTier.DEEP
        return json.dumps(
            {
                "schema": "insight-deep-dive-v1" if deep else "insight-narration-v1",
                "purpose": request.purpose,
                "output_class": request.output_class.value,
                "window_profile": _window_profile(case),
                "findings": [
                    {"name": finding.name, "value": decimal_figure(finding.value)}
                    for finding in case.findings
                ],
                "context": [],
                "silence_allowed": not deep,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    def _converse(
        self, request: InsightPromptCalibrationRunRequest, call: _CalibrationCall
    ) -> ConverseResponseTypeDef:
        evaluator = call.tool_name == _EVALUATOR_TOOL
        client = self.evaluator_client if evaluator else self.client
        model_id = (
            self.model_routes.quick_model_id
            if evaluator
            else self.model_routes.model_id_for(request.model_route)
        )
        effort_fields = (
            {"additionalModelRequestFields": {"output_config": {"effort": call.effort}}}
            if call.effort is not None
            else {}
        )
        try:
            return cast("BedrockRuntimeClient", client).converse(
                modelId=model_id,
                system=[{"text": call.system}],
                messages=[{"role": "user", "content": [{"text": call.body}]}],
                # No sampling parameters: the target model family refuses `temperature`
                # as deprecated (ADR-076 decision 3).
                inferenceConfig={"maxTokens": call.max_tokens},
                toolConfig={
                    "tools": [
                        {
                            "toolSpec": {
                                "name": call.tool_name,
                                "description": "Deliver the structured calibration output.",
                                "inputSchema": {"json": cast("Any", call.schema)},
                            }
                        }
                    ],
                    "toolChoice": {"tool": {"name": call.tool_name}},
                },
                **cast("Any", effort_fields),
            )
        except (ClientError, BotoCoreError) as error:
            raise DependencyError(
                _CALIBRATION_UNAVAILABLE,
                "Insight prompt calibration could not be run.",
                context=merged_aws_failure_context(
                    error,
                    {
                        "slot": request.slot_id.value,
                        "version": request.version,
                        "tier": request.tier.value,
                        "calibration_stage": "evaluator" if evaluator else "generation",
                    },
                ),
            ) from None

    @staticmethod
    def _require_completed(
        response: ConverseResponseTypeDef, request: InsightPromptCalibrationRunRequest
    ) -> None:
        """Type an incomplete call before a missing tool block decodes as malformed.

        A run cut by the token budget must not read as a model-behavior problem: the
        stop reason and usage are the diagnostics, mirroring the generator's
        truncation typing (the 2026-09-18 RCA lesson).
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
            _CALIBRATION_MALFORMED,
            "Insight calibration stopped before completing its tool call.",
            context={
                "slot": request.slot_id.value,
                "stop_reason": stop_reason,
                "usage": usage_context,
            },
        )

    @staticmethod
    def _tool_input(response: ConverseResponseTypeDef, tool_name: str) -> dict[str, object]:
        try:
            output = cast("dict[str, object]", response)["output"]
            message = cast("dict[str, object]", cast("dict[str, object]", output)["message"])
            content = cast("list[object]", message["content"])
            tool_uses = [
                cast("dict[str, object]", block)["toolUse"]
                for block in content
                if isinstance(block, dict) and "toolUse" in cast("dict[str, object]", block)
            ]
            if len(tool_uses) != 1:
                raise ValueError("calibration response must contain exactly one tool call.")
            tool_use = cast("dict[str, object]", tool_uses[0])
            if tool_use.get("name") != tool_name:
                raise ValueError("calibration response tool call is not the pinned tool.")
            payload = tool_use["input"]
            if not isinstance(payload, dict):
                raise ValueError("calibration response tool input is not a mapping.")
            return cast("dict[str, object]", payload)
        except (KeyError, TypeError, ValueError) as error:
            raise InvariantError(
                _CALIBRATION_MALFORMED,
                "Insight calibration returned malformed structured output.",
            ) from error
