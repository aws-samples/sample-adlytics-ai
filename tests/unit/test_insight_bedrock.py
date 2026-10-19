# SPDX-License-Identifier: MIT-0
"""Route-bound Bedrock insight model selection."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from decimal import Decimal
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError
from insight_key_support import (
    FICTIONAL_DEEP_INPUT_REVISION,
    PENDING_INPUT_DIGEST,
    card_key,
    rekeyed_generation,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from adapters.outbound.bedrock.insights import BedrockInsightGenerator, InsightModelRouteModels
from adapters.outbound.telemetry import tracing
from adapters.outbound.telemetry.span_context import correlated
from domain.presentation.insights import (
    DeterministicFinding,
    DeterministicFindingRequest,
    InsightCacheKey,
    InsightContextEntry,
    InsightFindingKind,
    InsightFindingOperand,
    InsightGenerationRequest,
    InsightHorizonRole,
    InsightModelRoute,
    InsightOutputClass,
    InsightPromptActivation,
    InsightPromptTier,
    InsightSlotId,
    InsightWindowProfile,
    calculate_deterministic_findings,
    insight_input_digest,
    insight_input_document,
)
from domain.presentation.temporal import TemporalDisplayGrain
from domain.shared_kernel.errors import DependencyError, InvariantError

if TYPE_CHECKING:
    from types_boto3_bedrock_runtime.client import BedrockRuntimeClient

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-068", "ADR-076")]


@dataclass
class ConverseClient:
    """Fake Converse returning one pinned toolUse block, the forced-tool-use shape."""

    calls: list[dict[str, object]] = field(default_factory=list[dict[str, object]])

    payload: dict[str, object] = field(
        default_factory=lambda: {
            "output_class": "perspective",
            "sentences": ["A governed finding matters."],
        }
    )
    stop_reason: str = "tool_use"
    tool_name: str = "insight_narration_v1"

    def converse(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        content: list[dict[str, object]] = (
            [
                {
                    "toolUse": {
                        "toolUseId": "tooluse-fictional",
                        "name": self.tool_name,
                        "input": self.payload,
                    }
                }
            ]
            if self.stop_reason == "tool_use"
            # A generation cut by the token budget never completes its tool call.
            else []
        )
        return {
            "output": {"message": {"content": content}},
            "stopReason": self.stop_reason,
            "usage": {"inputTokens": 210, "outputTokens": 64, "totalTokens": 274},
        }


@dataclass
class RaisingConverseClient:
    code: str

    def converse(self, **kwargs: object) -> dict[str, object]:
        del kwargs
        raise ClientError(
            cast(
                "Any",
                {
                    "Error": {"Code": self.code, "Message": "provider rejected request"},
                    "ResponseMetadata": {
                        "HTTPStatusCode": 400,
                        "RequestId": "request-fictional",
                    },
                },
            ),
            "Converse",
        )


def test_provider_schema_validation_is_an_invariant_not_a_dependency_retry() -> None:
    generator = BedrockInsightGenerator(
        InsightModelRouteModels("model-quick-fictional", "model-deep-fictional"),
        2,
        cast("BedrockRuntimeClient", RaisingConverseClient("ValidationException")),
    )

    with pytest.raises(InvariantError) as raised:
        generator.generate(_request(InsightModelRoute.QUICK))

    assert str(raised.value.code) == "ADLY-INSIGHT-0005"
    assert raised.value.context["service_code"] == "ValidationException"


def test_provider_transport_failure_remains_a_recoverable_dependency() -> None:
    generator = BedrockInsightGenerator(
        InsightModelRouteModels("model-quick-fictional", "model-deep-fictional"),
        2,
        cast("BedrockRuntimeClient", RaisingConverseClient("ThrottlingException")),
    )

    with pytest.raises(DependencyError) as raised:
        generator.generate(_request(InsightModelRoute.QUICK))

    assert str(raised.value.code) == "ADLY-INSIGHT-0004"
    assert raised.value.context["service_code"] == "ThrottlingException"


_PROFILE = InsightWindowProfile(
    days=7,
    grain=TemporalDisplayGrain.DAY,
    prior_start="2026-08-25",
    prior_end="2026-08-31",
    role=InsightHorizonRole.TACTICAL_REVIEW,
)


def _request(route: InsightModelRoute) -> InsightGenerationRequest:
    model_input = SimpleNamespace(
        output_class=InsightOutputClass.PERSPECTIVE,
        purpose="Explain one server-owned contribution.",
        findings=(DeterministicFinding("Fictional Campaign", Decimal("5")),),
        window_profile=_PROFILE,
        context=(
            InsightContextEntry("reporting window", "2026-09-01 to 2026-09-07"),
            InsightContextEntry("campaigns in view", "Fictional Campaign — fictional_channel"),
        ),
    )
    return InsightGenerationRequest(
        key=card_key(
            InsightCacheKey(
                analytical_scope_id="analytical-scope-v1:fictional",
                subject="campaign-fictional",
                window_start="2026-09-01",
                window_end="2026-09-07",
                artifact_revision="revision-fictional",
                prompt_version="v1",
                slot_id=InsightSlotId.CONTRIBUTION,
                input_digest=PENDING_INPUT_DIGEST,
            ),
            model_input,
        ),
        prompt=InsightPromptActivation(
            InsightSlotId.CONTRIBUTION,
            "v1",
            "A reviewed fictional prompt.",
            route,
        ),
        **vars(model_input),
    )


def test_generator_supplies_display_context_beside_exact_findings() -> None:
    """ADR-068 context depth: display facts inform wording; figures come from findings."""
    client = ConverseClient()
    generator = BedrockInsightGenerator(
        InsightModelRouteModels("model-quick-fictional", "model-deep-fictional"),
        2,
        cast("BedrockRuntimeClient", client),
    )
    request = _request(InsightModelRoute.QUICK)

    generator.generate(request)

    call = cast("dict[str, Any]", client.calls[0])
    messages = cast("list[dict[str, Any]]", call["messages"])
    payload = json.loads(cast("list[dict[str, str]]", messages[0]["content"])[0]["text"])
    assert payload["context"] == [
        {"name": "reporting window", "value": "2026-09-01 to 2026-09-07"},
        {"name": "campaigns in view", "value": "Fictional Campaign — fictional_channel"},
    ]
    assert payload["findings"] == [{"name": "Fictional Campaign", "value": "5"}]
    # The typed window profile every payload declares (ADR-076 decision 5): the
    # payload states the role; interpreting it is prompt work.
    assert payload["window_profile"] == {
        "days": 7,
        "grain": "day",
        "prior_start": "2026-08-25",
        "prior_end": "2026-08-31",
        "role": "tactical-review",
    }
    assert request.finding_values == ("5",)
    tool_config = cast("dict[str, Any]", call["toolConfig"])
    assert tool_config["toolChoice"] == {"tool": {"name": "insight_narration_v1"}}
    tools = cast("list[dict[str, Any]]", tool_config["tools"])
    assert len(tools) == 1
    # The card envelope is the output class and the sentences (ADR-082): no
    # citation list is declared, so none can be refused.
    schema = tools[0]["toolSpec"]["inputSchema"]["json"]
    assert schema["required"] == ["output_class", "sentences"]
    assert set(schema["properties"]) == {"output_class", "sentences"}
    # No sampling parameters travel: stability is content-identity caching, and the
    # target model family refuses temperature (ADR-076).
    assert call["inferenceConfig"] == {"maxTokens": 512}
    assert "outputConfig" not in call
    # The card tier keeps the model's default effort: its calibrated behavior is
    # frozen until the slice H cutover (the deep tier alone carries the dial).
    assert "additionalModelRequestFields" not in call


@pytest.mark.parametrize(
    ("route", "expected_model_id"),
    [
        (InsightModelRoute.QUICK, "model-quick-fictional"),
        (InsightModelRoute.DEEP, "model-deep-fictional"),
    ],
)
def test_generator_selects_only_the_model_configured_for_the_active_route(
    route: InsightModelRoute, expected_model_id: str
) -> None:
    client = ConverseClient()
    generator = BedrockInsightGenerator(
        InsightModelRouteModels("model-quick-fictional", "model-deep-fictional"),
        2,
        cast("BedrockRuntimeClient", client),
    )

    narration = generator.generate(_request(route))

    assert narration.output_class is InsightOutputClass.PERSPECTIVE
    assert client.calls[0]["modelId"] == expected_model_id


def _generator(client: ConverseClient) -> BedrockInsightGenerator:
    return BedrockInsightGenerator(
        InsightModelRouteModels("model-quick-fictional", "model-deep-fictional"),
        2,
        cast("BedrockRuntimeClient", client),
    )


@pytest.mark.parametrize("output_class", list(InsightOutputClass))
@pytest.mark.covers("ADR-082")
def test_narration_prose_is_never_text_checked_for_any_output_class(
    output_class: InsightOutputClass,
) -> None:
    """Derived figures and any wording pass through; only the structure is checked.

    ADR-082 removed the citation-subset and wire-token gates: a derived percentage,
    a figure the findings do not list, and an internal-looking word are all left to
    calibration and evaluation, never refused by pattern.
    """
    sentences = [
        "The fictional movement is up roughly 40 percent, about 2 more than before.",
        "This fictional slot reads as generation-pending.",
    ]
    client = ConverseClient(payload={"output_class": output_class.value, "sentences": sentences})
    request = rekeyed_generation(_request(InsightModelRoute.QUICK), output_class=output_class)

    narration = _generator(client).generate(request)

    assert narration.output_class is output_class
    assert narration.sentences == tuple(sentences)


def test_an_output_class_that_differs_from_the_declaration_is_refused() -> None:
    """The declared output class is the structural check that stays (ADR-082 §3)."""
    client = ConverseClient(
        payload={"output_class": "hypothesis", "sentences": ["A fictional proposal."]}
    )

    with pytest.raises(InvariantError) as raised:
        _generator(client).generate(_request(InsightModelRoute.QUICK))

    assert str(raised.value.code) == "ADLY-INSIGHT-0005"
    assert raised.value.context["violation"] == (
        "output class differs from the declared slot class"
    )


def test_payload_findings_render_in_the_canonical_figure_form() -> None:
    """Findings travel to the model as canonical decimals ("12.45", never "12.4500")."""
    client = ConverseClient(
        payload={
            "output_class": "perspective",
            "sentences": ["Fictional ad spend rose by 12.45."],
        }
    )
    request = rekeyed_generation(
        _request(InsightModelRoute.QUICK),
        findings=(DeterministicFinding("fictional ad spend change", Decimal("12.4500")),),
    )

    _generator(client).generate(request)

    call = cast("dict[str, Any]", client.calls[0])
    messages = cast("list[dict[str, Any]]", call["messages"])
    payload = json.loads(cast("list[dict[str, str]]", messages[0]["content"])[0]["text"])
    assert payload["findings"] == [{"name": "fictional ad spend change", "value": "12.45"}]
    assert request.finding_values == ("12.45",)


def test_generation_stopped_before_tool_use_is_a_typed_truncation_with_diagnostics() -> None:
    """A budget-truncated generation is typed, never a generic malformed-output error.

    The 2026-09-18 silent DLQ class was diagnosable only from the stop reason
    and token usage this error now carries. Under forced tool use the complete
    stop reason is tool_use; a max_tokens stop means the pinned tool call was cut.
    """
    client = ConverseClient(stop_reason="max_tokens")

    with pytest.raises(InvariantError) as raised:
        _generator(client).generate(_request(InsightModelRoute.QUICK))

    assert str(raised.value.code) == "ADLY-INSIGHT-0012"
    assert raised.value.context["stop_reason"] == "max_tokens"
    assert raised.value.context["slot"] == "contribution"
    assert raised.value.context["usage"] == {
        "inputTokens": 210,
        "outputTokens": 64,
        "totalTokens": 274,
    }


def test_malformed_structured_output_carries_a_bounded_violation_summary() -> None:
    """INSIGHT-0005 with empty context made log-only RCA impossible.

    The summary names this adapter's bounded validation message without echoing
    model text. The provider parses tool input, so the malformed classes are
    wrong field sets and wrong types, never undecodable JSON.
    """
    # The retired citation field is now an unexpected field like any other.
    client = ConverseClient(
        payload={
            "output_class": "perspective",
            "sentences": ["A fictional cut"],
            "cited_figures": [],
        }
    )

    with pytest.raises(InvariantError) as raised:
        _generator(client).generate(_request(InsightModelRoute.QUICK))

    assert str(raised.value.code) == "ADLY-INSIGHT-0005"
    assert raised.value.context["slot"] == "contribution"
    violation = raised.value.context["violation"]
    assert isinstance(violation, str)
    assert violation == "ValueError: structured response fields are not exact."
    assert "A fictional cut" not in violation


def test_a_second_tool_call_is_refused_as_ambiguous() -> None:
    """Two tool calls cannot both be the narration; ambiguity is refused, not chosen from."""

    @dataclass
    class TwoToolClient:
        def converse(self, **kwargs: object) -> dict[str, object]:
            del kwargs
            block = {
                "toolUse": {
                    "toolUseId": "tooluse-fictional",
                    "name": "insight_narration_v1",
                    "input": {
                        "output_class": "perspective",
                        "sentences": ["A governed finding matters."],
                    },
                }
            }
            return {
                "output": {"message": {"content": [block, dict(block)]}},
                "stopReason": "tool_use",
                "usage": {"inputTokens": 210, "outputTokens": 64, "totalTokens": 274},
            }

    generator = BedrockInsightGenerator(
        InsightModelRouteModels("model-quick-fictional", "model-deep-fictional"),
        2,
        cast("BedrockRuntimeClient", TwoToolClient()),
    )

    with pytest.raises(InvariantError) as raised:
        generator.generate(_request(InsightModelRoute.QUICK))

    assert str(raised.value.code) == "ADLY-INSIGHT-0005"
    assert raised.value.context["violation"] == (
        "ValueError: structured response must contain exactly one tool call."
    )


def test_a_tool_call_that_is_not_the_pinned_tool_is_refused() -> None:
    """Forced toolChoice makes any other tool name a provider contract violation."""
    client = ConverseClient(tool_name="fabricated_tool")

    with pytest.raises(InvariantError) as raised:
        _generator(client).generate(_request(InsightModelRoute.QUICK))

    assert str(raised.value.code) == "ADLY-INSIGHT-0005"
    assert raised.value.context["violation"] == (
        "ValueError: structured response tool call is not the pinned tool."
    )
    assert "fabricated_tool" not in str(raised.value.context["violation"])


def test_a_non_mapping_tool_input_is_refused() -> None:
    client = ConverseClient(payload=cast("dict[str, object]", "not-a-mapping"))

    with pytest.raises(InvariantError) as raised:
        _generator(client).generate(_request(InsightModelRoute.QUICK))

    assert str(raised.value.code) == "ADLY-INSIGHT-0005"
    assert raised.value.context["violation"] == (
        "ValueError: structured response tool input is not a mapping."
    )


def test_undeclared_output_class_reports_a_bounded_violation_without_model_text() -> None:
    client = ConverseClient(payload={"output_class": "fabricated-class", "sentences": []})

    with pytest.raises(InvariantError) as raised:
        _generator(client).generate(_request(InsightModelRoute.QUICK))

    assert str(raised.value.code) == "ADLY-INSIGHT-0005"
    assert raised.value.context["violation"] == (
        "ValueError: structured response output class is undeclared."
    )
    assert "fabricated-class" not in str(raised.value.context["violation"])


def test_rca_shaped_percentage_findings_render_within_the_structured_output_budget() -> None:
    """Quantized findings keep the 2026-09-18 payload class inside the budget.

    Four high-precision spend rates whose verbatim citation outgrew maxTokens
    now render as bounded citable figures.
    """
    operands = tuple(
        InsightFindingOperand(f"Fictional Flight {index:02d}", current, prior)
        for index, (current, prior) in enumerate(
            (
                (Decimal("8541.1200"), Decimal("15111.9400")),
                (Decimal("922.7700"), Decimal("1645.1300")),
                (Decimal("31007.0100"), Decimal("54873.4300")),
                (Decimal("77.3300"), Decimal("136.9900")),
            ),
            start=1,
        )
    )
    findings = calculate_deterministic_findings(
        DeterministicFindingRequest(InsightFindingKind.PERCENTAGE_CHANGE, operands)
    )
    request = rekeyed_generation(_request(InsightModelRoute.QUICK), findings=findings)
    client = ConverseClient()

    _generator(client).generate(request)

    call = cast("dict[str, list[dict[str, list[dict[str, str]]]]]", client.calls[0])
    body = call["messages"][0]["content"][0]["text"]
    payload = json.loads(body)
    rendered_values = [item["value"] for item in payload["findings"]]
    assert all(len(value) <= 8 for value in rendered_values)
    assert all(len(figure) <= 8 for figure in request.finding_values)
    assert len(body.encode("utf-8")) < 2_048


def _tracer_exporter(monkeypatch: pytest.MonkeyPatch) -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing, "TRACER", provider.get_tracer("test"))
    return exporter


def _generation_span_attributes(exporter: InMemorySpanExporter) -> dict[str, object]:
    spans = [s for s in exporter.get_finished_spans() if s.name == "insight.model.call"]
    assert len(spans) == 1
    return dict(spans[0].attributes or {})


def _captured_generation_span(
    monkeypatch: pytest.MonkeyPatch, client: ConverseClient
) -> dict[str, object]:
    exporter = _tracer_exporter(monkeypatch)
    _generator(client).generate(_request(InsightModelRoute.QUICK))
    return _generation_span_attributes(exporter)


def test_graph_generation_reports_bound_run_and_task_correlation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exporter = _tracer_exporter(monkeypatch)
    expected = {"run_id": "run-graph-fictional", "task_id": "precompute"}

    with correlated(expected["run_id"], expected["task_id"], "cohort-fictional"):
        _generator(ConverseClient()).generate(_request(InsightModelRoute.QUICK))

    attributes = _generation_span_attributes(exporter)
    actual = {name: attributes[name] for name in expected}
    assert actual == expected
    # Paired failing control: a model span carrying another task cannot satisfy A2.4.
    with pytest.raises(AssertionError):
        assert {**actual, "task_id": "another-task"} == expected


def test_every_generation_reports_its_span_with_latency_usage_and_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UX decisions about refresh-free insight delivery are made from this span.

    Every LLM call — serve-time, continuation, or precompute — must report latency,
    token usage, stop reason, and outcome under whichever boundary invoked it.
    """
    attributes = _captured_generation_span(monkeypatch, ConverseClient())

    assert attributes["slot_id"] == "contribution"
    assert attributes["model_route"] == "quick"
    assert attributes["model_id"] == "model-quick-fictional"
    assert attributes["configured_timeout_ms"] == 2_000
    assert attributes["observed_timeout"] is False
    # Every generation this adapter performs is the card tier until the deep-dive
    # request path lands (ADR-076); the attribute makes the tier a span-visible fact.
    assert attributes["tier"] == "card"
    # The horizon role rides the same span for serve, continuation, and precompute.
    assert attributes["horizon_role"] == "tactical-review"
    assert attributes["outcome"] == "generated"
    # No text check runs (ADR-082), so no verification attribute is recorded.
    assert "verification_outcome" not in attributes
    assert attributes["stop_reason"] == "tool_use"
    assert attributes["usage_input"] == 210
    assert attributes["usage_output"] == 64
    assert isinstance(attributes["total_millis"], int)
    assert attributes["total_millis"] >= 0


def test_a_timeout_is_observed_on_the_model_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TimeoutClient:
        def converse(self, **kwargs: object) -> dict[str, object]:
            del kwargs
            raise ReadTimeoutError(endpoint_url="https://bedrock.test.invalid")

    exporter = _tracer_exporter(monkeypatch)
    generator = BedrockInsightGenerator(
        InsightModelRouteModels("model-quick-fictional", "model-deep-fictional"),
        2,
        cast("BedrockRuntimeClient", TimeoutClient()),
    )

    with pytest.raises(DependencyError):
        generator.generate(_request(InsightModelRoute.QUICK))

    attributes = _generation_span_attributes(exporter)
    assert attributes["configured_timeout_ms"] == 2_000
    assert attributes["observed_timeout"] is True
    assert attributes["outcome"] == "timeout"


@pytest.mark.covers("ADR-076")
def test_the_model_input_is_the_domain_document_plus_the_tier_fields() -> None:
    """One home for the input shape: the payload and the card key's digest cannot drift."""
    client = ConverseClient()
    request = _request(InsightModelRoute.QUICK)
    _generator(client).generate(request)
    call = cast("dict[str, Any]", client.calls[0])
    payload = json.loads(call["messages"][0]["content"][0]["text"])

    assert payload.pop("schema") == "insight-narration-v1"
    assert payload.pop("silence_allowed") is True
    assert payload == insight_input_document(request)
    assert insight_input_digest(payload) == request.key.input_digest


@pytest.mark.covers("ADR-076")
def test_the_span_names_the_exact_input_and_cache_row_by_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ConverseClient()
    attributes = _captured_generation_span(monkeypatch, client)
    request = _request(InsightModelRoute.QUICK)
    sent = cast("dict[str, Any]", client.calls[0])["messages"][0]["content"][0]["text"]

    assert attributes["input_sha256"] == hashlib.sha256(sent.encode()).hexdigest()
    assert (
        attributes["cache_identity_sha256"]
        == hashlib.sha256(
            f"{request.key.ownership_key}\n{request.key.content_key}".encode()
        ).hexdigest()
    )
    # One changed context value changes the input digest the span records.
    edited = rekeyed_generation(
        request, context=(InsightContextEntry("reporting window", "2026-09-02 to 2026-09-07"),)
    )
    other = ConverseClient()
    exporter = _tracer_exporter(monkeypatch)
    _generator(other).generate(edited)
    edited_attributes = _generation_span_attributes(exporter)
    assert edited_attributes["input_sha256"] != attributes["input_sha256"]
    assert edited_attributes["cache_identity_sha256"] != attributes["cache_identity_sha256"]


def test_a_governed_silence_reports_the_silent_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Silence is the outcome the refresh-free-delivery analysis filters on."""
    silent = ConverseClient(payload={"output_class": "perspective", "sentences": []})
    attributes = _captured_generation_span(monkeypatch, silent)

    assert attributes["outcome"] == "silent"
    assert attributes["stop_reason"] == "tool_use"


def test_a_structural_refusal_still_records_usage_on_the_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Usage and stop reason are recorded before a structural check can raise."""
    exporter = _tracer_exporter(monkeypatch)
    client = ConverseClient(
        payload={"output_class": "hypothesis", "sentences": ["A fictional proposal."]}
    )

    with pytest.raises(InvariantError):
        _generator(client).generate(_request(InsightModelRoute.QUICK))

    attributes = _generation_span_attributes(exporter)
    assert attributes["tier"] == "card"
    assert attributes["stop_reason"] == "tool_use"
    assert attributes["usage_input"] == 210
    assert "outcome" not in attributes


@pytest.mark.covers("ADR-076")
def test_deep_generation_uses_the_opus_route_own_tool_schema_budget_and_tier_span() -> None:
    """A dialog click is a deep-route analysis, never a second card narration."""
    client = ConverseClient(
        payload={
            "what_stands_out": "The fictional contribution is concentrated at 5.",
            "candidate_readings": ["Reading one: the supplied movement may be temporary."],
            "discriminating_evidence": "Compare the next delivered window with 5.",
            "cited_figures": ["5"],
        },
        tool_name="insight_deep_dive_v1",
    )
    generator = BedrockInsightGenerator(
        InsightModelRouteModels("model-quick-fictional", "model-deep-fictional"),
        2,
        cast("BedrockRuntimeClient", client),
    )
    card = _request(InsightModelRoute.DEEP)
    request = replace(
        card,
        key=replace(
            card.key,
            tier=InsightPromptTier.DEEP,
            deep_input_revision=FICTIONAL_DEEP_INPUT_REVISION,
        ),
    )

    analysis = generator.generate_deep(request)

    assert analysis.cited_figures == ("5",)
    call = cast("dict[str, Any]", client.calls[0])
    assert call["modelId"] == "model-deep-fictional"
    assert call["inferenceConfig"] == {"maxTokens": 2500}
    # The deep tier caps the model's adaptive-thinking spend (product decision
    # 2026-09-25); the card tier must keep the model default.
    assert call["additionalModelRequestFields"] == {"output_config": {"effort": "medium"}}
    tool_config = cast("dict[str, Any]", call["toolConfig"])
    assert tool_config["toolChoice"] == {"tool": {"name": "insight_deep_dive_v1"}}
    messages = cast("list[dict[str, Any]]", call["messages"])
    payload = json.loads(cast("list[dict[str, str]]", messages[0]["content"])[0]["text"])
    assert payload["schema"] == "insight-deep-dive-v1"
    assert payload["silence_allowed"] is False


@pytest.mark.covers("ADR-076", "ADR-082")
def test_deep_citations_are_canonicalized_and_never_refused() -> None:
    """The deep list is kept for the stored and served shape; nothing gates it.

    Provider tool-input validation covers types, not uniqueness or nonblank strings,
    so structural noise is canonicalized; a value the findings do not carry is left
    to calibration and evaluation (ADR-082).
    """
    client = ConverseClient(
        payload={
            "what_stands_out": "The fictional contribution is 42.",
            "candidate_readings": ["Reading one: the movement may be temporary."],
            "discriminating_evidence": "Compare the next delivered window.",
            "cited_figures": ["5", "5", "   ", " 42 "],
        },
        tool_name="insight_deep_dive_v1",
    )
    generator = BedrockInsightGenerator(
        InsightModelRouteModels("model-quick-fictional", "model-deep-fictional"),
        2,
        cast("BedrockRuntimeClient", client),
    )
    card = _request(InsightModelRoute.DEEP)

    analysis = generator.generate_deep(
        replace(
            card,
            key=replace(
                card.key,
                tier=InsightPromptTier.DEEP,
                deep_input_revision=FICTIONAL_DEEP_INPUT_REVISION,
            ),
        )
    )

    assert analysis.cited_figures == ("5", "42")
