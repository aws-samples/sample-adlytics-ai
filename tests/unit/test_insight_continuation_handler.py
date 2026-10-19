# SPDX-License-Identifier: MIT-0
"""Durable insight continuation worker admission and retry behavior."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal
from types import SimpleNamespace
from typing import cast

import pytest
from insight_key_support import FICTIONAL_DEEP_INPUT_REVISION, PENDING_INPUT_DIGEST, card_key
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import adapters.jobs.insight_continuation.handler as worker
from adapters.inbound.presentation_handlers.insights import InsightSlotAssembler
from adapters.jobs.insight_continuation.handler import (
    InsightContinuationApplication,
    apply_records,
)
from adapters.outbound.dynamodb.insight_cache import (
    DynamoInsightCache,
    InsightCacheDegradeRequest,
)
from adapters.outbound.dynamodb.insight_prompt_registry import DynamoInsightPromptRegistry
from adapters.outbound.sqs.insight_continuation import serialize
from adapters.outbound.telemetry import tracing
from adapters.outbound.telemetry.tracing import INSIGHT_CONTINUATION
from domain.presentation.insights import (
    DeterministicFinding,
    InsightCacheKey,
    InsightCacheWriteResult,
    InsightContinuationRequest,
    InsightDeepCacheWriteResult,
    InsightDeepDiveState,
    InsightDeepRecord,
    InsightGenerationRequest,
    InsightHorizonRole,
    InsightModelRoute,
    InsightOutputClass,
    InsightPromptActivation,
    InsightPromptTier,
    InsightSlot,
    InsightSlotId,
    InsightSlotState,
    InsightWindowProfile,
)
from domain.presentation.temporal import TemporalDisplayGrain
from domain.shared_kernel.errors import ErrorCode, InvariantError

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-068")]


@dataclass
class Application:
    bodies: list[object] = field(default_factory=list[object])
    final_receives: list[bool] = field(default_factory=list[bool])
    fail: bool = False

    def apply(self, body: object, *, final_receive: bool = False) -> None:
        self.bodies.append(body)
        self.final_receives.append(final_receive)
        if self.fail:
            raise RuntimeError("dependency unavailable")


def test_worker_ignores_malformed_delivery_and_acknowledges_completed_messages() -> None:
    application = Application()

    failures = apply_records({"Records": [{"messageId": "one", "body": "{}"}]}, application, 3)

    assert failures == ()
    assert application.bodies == ["{}"]
    assert application.final_receives == [False]


def test_worker_returns_only_failed_message_identity_for_sqs_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = Application(fail=True)
    reported: list[BaseException] = []

    def capture_error(error: BaseException, _report: object) -> None:
        reported.append(error)

    monkeypatch.setattr(worker, "report_error", capture_error)

    failures = apply_records({"Records": [{"messageId": "one", "body": "valid"}]}, application, 3)

    assert failures == ("one",)
    assert len(reported) == 1
    assert isinstance(reported[0], RuntimeError)


def test_worker_marks_only_the_exhausted_receive_as_final() -> None:
    application = Application()

    event: dict[str, object] = {
        "Records": [
            {
                "messageId": "second",
                "body": "{}",
                "attributes": {"ApproximateReceiveCount": "2"},
            },
            {
                "messageId": "third",
                "body": "{}",
                "attributes": {"ApproximateReceiveCount": "3"},
            },
            {"messageId": "unstamped", "body": "{}", "attributes": {}},
        ]
    }

    apply_records(event, application, 3)

    assert application.final_receives == [False, True, False]


class _Context:
    function_name = "insight-continuation"
    function_version = "$LATEST"
    invoked_function_arn = "arn:aws:lambda:us-east-1:111122223333:function:continuation"
    memory_limit_in_mb = 512
    aws_request_id = "request-1"


def test_handler_records_batch_disposition_and_reports_failure_without_message_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing, "TRACER", provider.get_tracer("test"))
    application = Application(fail=True)
    reported: list[BaseException] = []

    def capture_error(error: BaseException, _report: object) -> None:
        reported.append(error)

    monkeypatch.setattr(worker, "application", lambda: application)
    monkeypatch.setattr(
        worker,
        "insight_continuation_worker_settings",
        lambda: SimpleNamespace(insight_continuation_max_receive_count=3),
    )
    monkeypatch.setattr(worker, "report_error", capture_error)

    result = worker.handler(
        {"Records": [{"body": "missing-id"}, {"messageId": "retry", "body": "valid"}]},
        _Context(),
    )

    assert result == {"batchItemFailures": [{"itemIdentifier": "retry"}]}
    assert len(reported) == 2
    (root,) = [item for item in exporter.get_finished_spans() if item.name == INSIGHT_CONTINUATION]
    assert dict(root.attributes or {}) == {
        "operation": INSIGHT_CONTINUATION,
        "error_type": "RuntimeError",
        "outcome": "partial_retry",
        "record_count": 2,
        "acknowledged_count": 1,
        "failed_count": 1,
    }


def _generation() -> InsightGenerationRequest:
    model_input = SimpleNamespace(
        output_class=InsightOutputClass.PERSPECTIVE,
        purpose="Explain one server-owned contribution.",
        findings=(DeterministicFinding("Fictional Campaign", Decimal("5")),),
        window_profile=InsightWindowProfile(
            days=7,
            grain=TemporalDisplayGrain.DAY,
            prior_start="2026-08-25",
            prior_end="2026-08-31",
            role=InsightHorizonRole.TACTICAL_REVIEW,
        ),
        context=(),
    )
    return InsightGenerationRequest(
        key=card_key(
            InsightCacheKey(
                analytical_scope_id="analytical-scope-v1:fictional",
                subject="campaign-portfolio",
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
            InsightModelRoute.QUICK,
        ),
        **vars(model_input),
    )


@dataclass
class _Prompts:
    def version(self, slot_id: InsightSlotId, version: str) -> InsightPromptActivation:
        return InsightPromptActivation(
            slot_id, version, "A reviewed fictional prompt.", InsightModelRoute.QUICK
        )


@dataclass
class _FailingAssembler:
    error: Exception

    def fill(self, _generation: InsightGenerationRequest) -> None:
        raise self.error


@dataclass
class _DegradeCache:
    requests: list[InsightCacheDegradeRequest] = field(
        default_factory=list[InsightCacheDegradeRequest]
    )

    def put_unavailable_if_absent(
        self, request: InsightCacheDegradeRequest
    ) -> InsightCacheWriteResult:
        self.requests.append(request)
        slot = InsightSlot(
            id=request.key.slot_id,
            output_class=request.output_class,
            state=InsightSlotState.UNAVAILABLE,
            text=None,
            evidence_line=None,
            artifact_revision=request.key.artifact_revision,
            prompt_version=request.key.prompt_version,
        )
        return InsightCacheWriteResult(stored=True, slot=slot)


def _application(cache: _DegradeCache, error: Exception) -> InsightContinuationApplication:
    return InsightContinuationApplication(
        prompts=cast("DynamoInsightPromptRegistry", _Prompts()),
        assembler=cast("InsightSlotAssembler", _FailingAssembler(error)),
        cache=cast("DynamoInsightCache", cache),
    )


def test_final_receive_degrades_to_a_durable_unavailable_row_with_the_typed_reason() -> None:
    """A deterministically failing generation becomes a visible served absence.

    Never a silent DLQ member: that was the 2026-09-18 invisibility class.
    """
    cache = _DegradeCache()
    body = serialize(InsightContinuationRequest(_generation()))
    error = InvariantError(
        ErrorCode("INSIGHT", 12), "Insight generation stopped before completing."
    )

    _application(cache, error).apply(body, final_receive=True)

    (request,) = cache.requests
    assert request.key.slot_id is InsightSlotId.CONTRIBUTION
    assert request.reason == "ADLY-INSIGHT-0012"


def test_untyped_final_failure_degrades_with_a_bounded_untyped_reason() -> None:
    cache = _DegradeCache()
    body = serialize(InsightContinuationRequest(_generation()))

    _application(cache, RuntimeError("socket closed")).apply(body, final_receive=True)

    (request,) = cache.requests
    assert request.reason == "untyped_RuntimeError"


def test_nonfinal_receive_still_raises_for_sqs_retry_and_writes_no_degrade_row() -> None:
    cache = _DegradeCache()
    body = serialize(InsightContinuationRequest(_generation()))

    with pytest.raises(RuntimeError):
        _application(cache, RuntimeError("transient")).apply(body, final_receive=False)

    assert cache.requests == []


@dataclass
class _RetiredPrompts:
    def version(self, slot_id: InsightSlotId, version: str) -> None:
        del slot_id, version


def test_missing_immutable_prompt_version_degrades_durably_instead_of_acknowledging() -> None:
    """The prompt-miss acknowledgement was the RCA invisibility class's last instance.

    Without the degrade row there is no cache row, no DLQ member, and no alarm.
    """
    cache = _DegradeCache()
    application = InsightContinuationApplication(
        prompts=cast("DynamoInsightPromptRegistry", _RetiredPrompts()),
        assembler=cast("InsightSlotAssembler", _FailingAssembler(RuntimeError("unreached"))),
        cache=cast("DynamoInsightCache", cache),
    )
    body = serialize(InsightContinuationRequest(_generation()))

    application.apply(body, final_receive=False)

    (request,) = cache.requests
    assert request.reason == "prompt_version_unavailable"
    assert request.key.prompt_version == "v1"


@dataclass
class _TierRecordingAssembler:
    filled: list[InsightGenerationRequest] = field(default_factory=list[InsightGenerationRequest])
    deep_filled: list[InsightGenerationRequest] = field(
        default_factory=list[InsightGenerationRequest]
    )

    def fill(self, generation: InsightGenerationRequest) -> None:
        self.filled.append(generation)

    def fill_deep(self, generation: InsightGenerationRequest) -> None:
        self.deep_filled.append(generation)


@dataclass
class _DeepDegradeCache:
    records: list[tuple[InsightDeepRecord, InsightCacheKey]] = field(
        default_factory=list[tuple[InsightDeepRecord, InsightCacheKey]]
    )

    def put_deep_if_absent(
        self, record: InsightDeepRecord, key: InsightCacheKey
    ) -> InsightDeepCacheWriteResult:
        self.records.append((record, key))
        return InsightDeepCacheWriteResult(stored=True, record=record)


def _deep_generation() -> InsightGenerationRequest:
    generation = _generation()
    return replace(
        generation,
        key=replace(
            generation.key,
            tier=InsightPromptTier.DEEP,
            deep_input_revision=FICTIONAL_DEEP_INPUT_REVISION,
        ),
    )


@pytest.mark.covers("ADR-076")
def test_a_deep_tier_message_routes_to_fill_deep_with_its_immutable_prompt() -> None:
    """The codec v4 tier decides the assembler entry point.

    A card fill for a deep key would write a card-shaped row under a deep identity.
    """
    assembler = _TierRecordingAssembler()
    application = InsightContinuationApplication(
        prompts=cast("DynamoInsightPromptRegistry", _Prompts()),
        assembler=cast("InsightSlotAssembler", assembler),
        cache=cast("DynamoInsightCache", _DegradeCache()),
    )
    body = serialize(InsightContinuationRequest(_deep_generation()))

    application.apply(body)

    assert assembler.filled == []
    (generation,) = assembler.deep_filled
    assert generation.key.tier is InsightPromptTier.DEEP
    assert generation.key.slot_id is InsightSlotId.CONTRIBUTION
    assert generation.prompt.version == "v1"


@dataclass
class _DeepFailingAssembler:
    error: Exception

    def fill_deep(self, _generation: InsightGenerationRequest) -> None:
        raise self.error


@pytest.mark.covers("ADR-076")
def test_deep_final_receive_degrades_to_a_durable_deep_placeholder() -> None:
    """Retry exhaustion writes the deep-shaped UNAVAILABLE record under the deep key.

    The serve path overlays governed wording on the stored reason and later heals it.
    """
    cache = _DeepDegradeCache()
    application = InsightContinuationApplication(
        prompts=cast("DynamoInsightPromptRegistry", _Prompts()),
        assembler=cast(
            "InsightSlotAssembler",
            _DeepFailingAssembler(
                InvariantError(ErrorCode("INSIGHT", 12), "Generation stopped early.")
            ),
        ),
        cache=cast("DynamoInsightCache", cache),
    )
    body = serialize(InsightContinuationRequest(_deep_generation()))

    application.apply(body, final_receive=True)

    ((record, key),) = cache.records
    assert key.tier is InsightPromptTier.DEEP
    assert record.state is InsightDeepDiveState.UNAVAILABLE
    assert record.analysis is None
    assert record.absence_reason == "ADLY-INSIGHT-0012"
    assert record.artifact_revision == "revision-fictional"
    assert record.prompt_version == "v1"
