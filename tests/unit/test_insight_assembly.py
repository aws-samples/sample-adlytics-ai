# SPDX-License-Identifier: MIT-0
"""Bounded cache-or-generate insight slot assembly behavior."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from threading import Barrier, Event
from time import sleep
from typing import Any, cast

import pytest
from insight_key_support import PENDING_INPUT_DIGEST, with_card_digest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import adapters.inbound.presentation_handlers.insights as insights_module
from adapters.deep_input_revision import DEEP_INPUT_REVISION
from adapters.inbound.presentation_handlers.insights import (
    InsightAssemblyRequest,
    InsightSlotAssembler,
)
from adapters.insight_requests import (
    DeclaredInsightRequestInput,
    InsightRequestContext,
    declared_insight_requests,
)
from adapters.jobs.contract_validation.insight_loader import load_insight_contracts
from adapters.outbound.telemetry import tracing
from adapters.outbound.telemetry.span_context import SpanCorrelation, correlated, correlation
from domain.presentation.insights import (
    MAX_INSIGHT_EVIDENCE_BYTES,
    MAX_INSIGHT_SLOT_TEXT_BYTES,
    DeterministicFinding,
    FindingUnit,
    InsightAbsenceClass,
    InsightCacheKey,
    InsightCacheOutcome,
    InsightCacheWriteResult,
    InsightDeepAnalysis,
    InsightDeepCacheWriteResult,
    InsightDeepDiveState,
    InsightDeepRecord,
    InsightEvidenceBundle,
    InsightEvidenceInput,
    InsightFindingKind,
    InsightGeneratedNarration,
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
    MetricScale,
    OperandBasis,
    PredicateFinding,
)
from domain.presentation.temporal import TemporalDisplayGrain
from domain.shared_kernel.errors import DependencyError, ErrorCode, InvariantError
from domain.shared_kernel.values import DateRange

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-068")]

_PROFILE = InsightWindowProfile(
    days=7,
    grain=TemporalDisplayGrain.DAY,
    prior_start="2026-07-25",
    prior_end="2026-07-31",
    role=InsightHorizonRole.TACTICAL_REVIEW,
)


@dataclass
class Cache:
    values: dict[InsightCacheKey, InsightSlot] = field(
        default_factory=dict[InsightCacheKey, InsightSlot]
    )
    write_events: dict[InsightCacheKey, Event] = field(default_factory=dict[InsightCacheKey, Event])

    def get(self, key: InsightCacheKey) -> InsightSlot | None:
        return self.values.get(key)

    def put_if_absent(self, slot: InsightSlot, key: InsightCacheKey) -> InsightCacheWriteResult:
        existing = self.values.get(key)
        replaces_placeholder = (
            existing is not None
            and existing.state is InsightSlotState.UNAVAILABLE
            and slot.state is not InsightSlotState.UNAVAILABLE
        )
        if existing is None or replaces_placeholder:
            self.values[key] = slot
            self.write_events.setdefault(key, Event()).set()
            return InsightCacheWriteResult(stored=True, slot=slot)
        self.write_events.setdefault(key, Event()).set()
        return InsightCacheWriteResult(stored=False, slot=existing)


@dataclass
class Prompts:
    activation: InsightPromptActivation | None

    def active(
        self, slot_id: InsightSlotId, tier: InsightPromptTier
    ) -> InsightPromptActivation | None:
        del slot_id, tier
        return self.activation


@dataclass
class PromptsForEachSlot:
    def active(self, slot_id: InsightSlotId, tier: InsightPromptTier) -> InsightPromptActivation:
        del tier
        return InsightPromptActivation(slot_id, "v1", "Prompt.", InsightModelRoute.QUICK)


class _NoDeep:
    """Structural port completion for card-only fakes; deep tests carry their own fake."""

    def generate_deep(self, request: InsightGenerationRequest) -> InsightDeepAnalysis:
        del request
        raise NotImplementedError("deep generation is not exercised by this fake.")


@dataclass
class Generator(_NoDeep):
    calls: int = 0

    def generate(self, request: InsightGenerationRequest) -> InsightGeneratedNarration:
        del request
        self.calls += 1
        return InsightGeneratedNarration(
            InsightOutputClass.PERSPECTIVE,
            ("A governed finding matters.",),
        )


@dataclass
class TracingGenerator(_NoDeep):
    correlations: list[SpanCorrelation | None] = field(default_factory=list[SpanCorrelation | None])

    def generate(self, request: InsightGenerationRequest) -> InsightGeneratedNarration:
        with tracing.span(tracing.INSIGHT_GENERATION):
            self.correlations.append(correlation())
            return InsightGeneratedNarration(
                request.output_class,
                ("A governed finding matters.",),
            )


def _request() -> InsightAssemblyRequest:
    key = InsightCacheKey(
        analytical_scope_id="analytical-scope-v1:scope-fictional",
        subject="campaign-fictional",
        window_start="2026-08-01",
        window_end="2026-08-07",
        artifact_revision="revision-fictional",
        prompt_version="v1",
        slot_id=InsightSlotId.CONTRIBUTION,
        input_digest=PENDING_INPUT_DIGEST,
    )
    return with_card_digest(
        InsightAssemblyRequest(
            key=key,
            output_class=InsightOutputClass.PERSPECTIVE,
            purpose="Explain one server-owned contribution.",
            findings=(DeterministicFinding("Fictional Campaign", Decimal("5")),),
            silence_reason="No fictional contribution exceeds the declared threshold.",
            window_profile=_PROFILE,
        )
    )


def test_assembly_reads_the_exact_revision_cache_entry_before_generation() -> None:
    request = _request()
    cache = Cache()
    generator = Generator()
    assembler = InsightSlotAssembler(
        cache,
        Prompts(
            InsightPromptActivation(
                InsightSlotId.CONTRIBUTION, "v1", "Prompt.", InsightModelRoute.QUICK
            )
        ),
        generator,
        2,
    )

    first, second = assembler.one(request), assembler.one(request)

    assert first == second
    assert first.artifact_revision == "revision-fictional"
    assert first.prompt_version == "v1"
    assert generator.calls == 1


def test_assembly_degrades_missing_activation_without_calling_the_model() -> None:
    generator = Generator()
    slot = InsightSlotAssembler(Cache(), Prompts(None), generator, 1).one(_request())

    assert slot.id is InsightSlotId.CONTRIBUTION
    assert slot.state.value == "unavailable"
    assert slot.artifact_revision == "revision-fictional"
    assert generator.calls == 0


@dataclass
class RaisingGenerator(_NoDeep):
    calls: int = 0

    def generate(self, request: InsightGenerationRequest) -> InsightGeneratedNarration:
        del request
        self.calls += 1
        raise RuntimeError("provider unavailable")


@dataclass
class SilentGenerator(_NoDeep):
    def generate(self, request: InsightGenerationRequest) -> InsightGeneratedNarration:
        return InsightGeneratedNarration(request.output_class, ())


def test_assembly_resolves_the_active_pointer_before_exact_cache_lookup() -> None:
    request = _request()
    cache = Cache()
    generator = Generator()
    assembler = InsightSlotAssembler(
        cache,
        Prompts(
            InsightPromptActivation(
                InsightSlotId.CONTRIBUTION, "v2", "Prompt.", InsightModelRoute.DEEP
            )
        ),
        generator,
        1,
    )

    slot = assembler.one(request)

    assert slot.prompt_version == "v2"
    assert tuple(cache.values) == (
        InsightCacheKey(
            analytical_scope_id="analytical-scope-v1:scope-fictional",
            subject="campaign-fictional",
            window_start="2026-08-01",
            window_end="2026-08-07",
            artifact_revision="revision-fictional",
            prompt_version="v2",
            slot_id=InsightSlotId.CONTRIBUTION,
            input_digest=request.key.input_digest,
        ),
    )
    assert generator.calls == 1


def test_assembly_degrades_generation_failure_without_exposing_partial_text() -> None:
    generator = RaisingGenerator()

    slot = InsightSlotAssembler(
        Cache(),
        Prompts(
            InsightPromptActivation(
                InsightSlotId.CONTRIBUTION, "v1", "Prompt.", InsightModelRoute.QUICK
            )
        ),
        generator,
        1,
    ).one(_request())

    assert slot.state.value == "unavailable"
    assert slot.text is None
    assert slot.evidence_line is None
    # A failed generation is a recoverable delivery state, never an unexplained blank.
    assert slot.absence_class is InsightAbsenceClass.GENERATION_PENDING
    assert slot.absence_reason is not None
    assert generator.calls == 1


def test_assembly_preserves_a_deliberate_silent_model_result() -> None:
    slot = InsightSlotAssembler(
        Cache(),
        Prompts(
            InsightPromptActivation(
                InsightSlotId.CONTRIBUTION, "v1", "Prompt.", InsightModelRoute.QUICK
            )
        ),
        SilentGenerator(),
        1,
    ).one(_request())

    assert slot.state.value == "silent"
    assert slot.text is None
    assert slot.prompt_version == "v1"
    # A model that evaluated the findings and declined is evaluated-quiet, and the
    # served slot names the contract's silence condition, never a generic fallback.
    assert slot.absence_class is InsightAbsenceClass.EVALUATED_QUIET
    assert slot.absence_reason == "No fictional contribution exceeds the declared threshold."


@dataclass
class ParallelGenerator(_NoDeep):
    barrier: Barrier = field(default_factory=lambda: Barrier(2))
    calls: list[InsightSlotId] = field(default_factory=list[InsightSlotId])

    def generate(self, request: InsightGenerationRequest) -> InsightGeneratedNarration:
        self.calls.append(request.key.slot_id)
        # A serial implementation times out here; both independent calls must overlap.
        self.barrier.wait(timeout=1)
        sleep(0.02)
        return InsightGeneratedNarration(request.output_class, ("A governed finding matters.",))


@dataclass
class DependencyRaisingGenerator(_NoDeep):
    calls: int = 0

    def generate(self, request: InsightGenerationRequest) -> InsightGeneratedNarration:
        del request
        self.calls += 1
        raise DependencyError(ErrorCode("INSIGHT", 4), "provider unavailable")


@dataclass
class InvariantRaisingGenerator(_NoDeep):
    calls: int = 0

    def generate(self, request: InsightGenerationRequest) -> InsightGeneratedNarration:
        del request
        self.calls += 1
        raise InvariantError(ErrorCode("INSIGHT", 5), "provider output is malformed")


@dataclass
class Continuations:
    requests: list[object] = field(default_factory=list[object])
    fail: bool = False

    def enqueue(self, request: object) -> str:
        if self.fail:
            raise RuntimeError("queue unavailable")
        self.requests.append(request)
        return "message-fictional"


def _cached_slot(key: InsightCacheKey) -> InsightSlot:
    return InsightSlot(
        key.slot_id,
        InsightOutputClass.PERSPECTIVE,
        InsightSlotState.FILLED,
        "A cached governed finding matters.",
        "Read from the values this page already shows.",
        key.artifact_revision,
        key.prompt_version,
    )


def test_many_returns_an_exact_cache_hit_without_generation_or_handoff() -> None:
    request = _request()
    active_key = replace(request.key, prompt_version="v1")
    cache = Cache(values={active_key: _cached_slot(active_key)})
    generator = Generator()
    continuation = Continuations()

    slot = InsightSlotAssembler(cache, PromptsForEachSlot(), generator, 2, continuation).many(
        (request,)
    )[0]

    assert slot == cache.values[active_key]
    assert generator.calls == 0
    assert continuation.requests == []


def test_cache_hit_silent_slot_from_before_the_taxonomy_gains_its_declared_absence() -> None:
    """A cached seven-field silent row must never serve an unexplained blank."""
    request = _request()
    active_key = replace(request.key, prompt_version="v1")
    legacy_silent = InsightSlot(
        active_key.slot_id,
        InsightOutputClass.PERSPECTIVE,
        InsightSlotState.SILENT,
        None,
        None,
        active_key.artifact_revision,
        active_key.prompt_version,
    )
    cache = Cache(values={active_key: legacy_silent})
    generator = Generator()

    slot = InsightSlotAssembler(cache, PromptsForEachSlot(), generator, 2).many((request,))[0]

    assert generator.calls == 0
    assert slot.state is InsightSlotState.SILENT
    assert slot.absence_class is InsightAbsenceClass.EVALUATED_QUIET
    assert slot.absence_reason == request.silence_reason


def test_many_runs_independent_misses_in_parallel_and_joins_in_request_order() -> None:
    request = _request()
    second = replace(request, key=replace(request.key, slot_id=InsightSlotId.METRIC_FOCUS))
    generator = ParallelGenerator()

    slots = InsightSlotAssembler(Cache(), PromptsForEachSlot(), generator, 2).many(
        (request, second), correlation_id="00000000-0000-4000-8000-000000000000"
    )

    assert [slot.id for slot in slots] == [
        InsightSlotId.CONTRIBUTION,
        InsightSlotId.METRIC_FOCUS,
    ]
    assert all(slot.state is InsightSlotState.FILLED for slot in slots)
    assert set(generator.calls) == {InsightSlotId.CONTRIBUTION, InsightSlotId.METRIC_FOCUS}


def test_many_propagates_graph_precompute_trace_and_correlation_to_model_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing, "TRACER", provider.get_tracer("test"))
    generator = TracingGenerator()
    run_id, task_id, cohort = "run-fictional", "precompute-fictional", "cohort-fictional"

    with correlated(run_id, task_id, cohort), tracing.span(tracing.PRECOMPUTE_RUN):
        slots = InsightSlotAssembler(Cache(), PromptsForEachSlot(), generator, 1).many(
            (_request(),), correlation_id=run_id
        )

    assert slots[0].state is InsightSlotState.FILLED
    spans = {item.name: item for item in exporter.get_finished_spans()}
    run_span = spans[tracing.PRECOMPUTE_RUN]
    model_span = spans[tracing.INSIGHT_GENERATION]
    run_context = run_span.context
    model_context = model_span.context
    model_parent = model_span.parent
    assert run_context is not None
    assert model_context is not None
    lineage = (
        model_parent.span_id if model_parent is not None else None,
        model_context.trace_id,
        generator.correlations,
    )
    expected = (
        run_context.span_id,
        run_context.trace_id,
        [SpanCorrelation(run_id, task_id, cohort)],
    )
    assert lineage == expected

    # Paired failing control: a detached worker has no parent and no bake correlation.
    with pytest.raises(AssertionError):
        assert (None, model_context.trace_id, [None]) == expected


def test_parallel_terminal_evidence_contains_directly_overlapping_slot_intervals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[Any] = []

    def record_operation(event: Any, _logger: Any) -> None:
        events.append(event)

    monkeypatch.setattr(insights_module, "report_operation", record_operation)
    request = _request()
    second = replace(request, key=replace(request.key, slot_id=InsightSlotId.METRIC_FOCUS))

    InsightSlotAssembler(Cache(), PromptsForEachSlot(), ParallelGenerator(), 2).many(
        (request, second), correlation_id="00000000-0000-4000-8000-000000000000"
    )

    slot_events = [event for event in events if event.phase == "slot"]
    assert len(slot_events) == 2
    intervals = [
        (
            cast("int", event.details["start_offset_ms"]),
            cast("int", event.details["end_offset_ms"]),
        )
        for event in slot_events
    ]
    assert max(start for start, _ in intervals) < min(end for _, end in intervals)
    batch = next(event for event in events if event.phase == "batch")
    assert batch.details["horizon_role"] == "tactical-review"
    assert batch.details["admitted_count"] == 2
    assert batch.details["generated_count"] == 2
    assert batch.details["cache_hit_count"] == 0
    assert batch.details["failed_count"] == 0
    assert batch.details["configured_concurrency"] == 2
    assert batch.details["maximum_model_concurrency"] == 2

    # Paired failing control: losing one observed concurrent call fails the same assertion.
    altered = {**batch.details, "maximum_model_concurrency": 1}
    with pytest.raises(AssertionError):
        assert altered["maximum_model_concurrency"] == 2


def _slot_log_event(
    monkeypatch: pytest.MonkeyPatch, generator: object, cache: Cache | None = None
) -> Any:
    events: list[Any] = []

    def record_operation(event: Any, _logger: Any) -> None:
        events.append(event)

    monkeypatch.setattr(insights_module, "report_operation", record_operation)
    InsightSlotAssembler(cache or Cache(), PromptsForEachSlot(), cast("Any", generator), 1).many(
        (_request(),), correlation_id="00000000-0000-4000-8000-000000000000"
    )
    return next(event for event in events if event.phase == "slot")


@pytest.mark.covers("ADR-076", "ADR-082")
def test_slot_log_records_lifecycle_outcomes_and_no_verifier_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The slot log keeps the lifecycle outcome; no text-check outcome exists (ADR-082)."""
    generated = _slot_log_event(monkeypatch, Generator())
    silent = _slot_log_event(monkeypatch, SilentGenerator())
    assert generated.outcome == "filled"
    assert silent.outcome == "silent"
    assert "verifier_outcome" not in generated.details
    # The horizon role is a queryable slot-log fact beside tier (ADR-076 decision 5).
    assert generated.details["horizon_role"] == "tactical-review"
    # An activation without composed provenance stamps the log as unsourced.
    assert generated.details["fragment_digest"] == "unsourced"


@dataclass
class ComposedPrompts:
    """An activation carrying composed fragment provenance (ADR-076 decision 6)."""

    def active(self, slot_id: InsightSlotId, tier: InsightPromptTier) -> InsightPromptActivation:
        del tier
        return InsightPromptActivation(
            slot_id, "v3", "Prompt.", InsightModelRoute.QUICK, fragment_digest="e" * 64
        )


@pytest.mark.covers("ADR-076")
def test_slot_log_stamps_the_composed_fragment_digest_when_one_generates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[Any] = []

    def record_operation(event: Any, _logger: Any) -> None:
        events.append(event)

    monkeypatch.setattr(insights_module, "report_operation", record_operation)
    InsightSlotAssembler(Cache(), ComposedPrompts(), Generator(), 1).many(
        (_request(),), correlation_id="00000000-0000-4000-8000-000000000000"
    )

    slot_event = next(event for event in events if event.phase == "slot")
    assert slot_event.details["fragment_digest"] == "e" * 64
    assert slot_event.details["prompt_version"] == "v3"


@pytest.mark.covers("ADR-076")
def test_a_cache_hit_and_an_invariant_failure_keep_their_lifecycle_outcomes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    active_key = replace(request.key, prompt_version="v1")
    cache_hit = _slot_log_event(
        monkeypatch, Generator(), Cache(values={active_key: _cached_slot(active_key)})
    )
    other_invariant = _slot_log_event(monkeypatch, InvariantRaisingGenerator())
    assert cache_hit.details["cache_outcome"] == "hit"
    assert cache_hit.details["generated"] is False
    assert other_invariant.outcome == "invariant_failed"


def test_telemetry_failure_cannot_change_or_hang_completed_slot_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Observability is never a serving dependency, even after all worker work completed."""

    def fail_report(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("telemetry unavailable")

    monkeypatch.setattr(insights_module, "report_operation", fail_report)
    monkeypatch.setattr(insights_module, "report_error", fail_report)

    slots = InsightSlotAssembler(Cache(), PromptsForEachSlot(), Generator(), 2).many(
        (_request(),), correlation_id="00000000-0000-4000-8000-000000000000"
    )

    assert len(slots) == 1
    assert slots[0].state is InsightSlotState.FILLED


def test_error_reporter_failure_cannot_change_or_hang_dependency_failure_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken error exporter cannot prevent the unavailable slot from joining."""

    def fail_report(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("telemetry unavailable")

    monkeypatch.setattr(insights_module, "report_error", fail_report)

    slots = InsightSlotAssembler(
        Cache(), PromptsForEachSlot(), DependencyRaisingGenerator(), 1
    ).many((_request(),), correlation_id="00000000-0000-4000-8000-000000000000")

    assert len(slots) == 1
    assert slots[0].state is InsightSlotState.UNAVAILABLE


def test_dependency_failure_hands_off_the_exact_immutable_request() -> None:
    generator = DependencyRaisingGenerator()
    continuation = Continuations()

    slot = InsightSlotAssembler(Cache(), PromptsForEachSlot(), generator, 1, continuation).one(
        _request()
    )

    assert slot.state is InsightSlotState.UNAVAILABLE
    assert generator.calls == 1
    assert len(continuation.requests) == 1
    admitted = cast("Any", continuation.requests[0])
    assert admitted.generation.key == replace(_request().key, prompt_version="v1")


def test_invariant_failure_is_not_blindly_retried_in_the_background() -> None:
    generator = InvariantRaisingGenerator()
    continuation = Continuations()

    slot = InsightSlotAssembler(Cache(), PromptsForEachSlot(), generator, 1, continuation).one(
        _request()
    )

    assert slot.state is InsightSlotState.UNAVAILABLE
    assert generator.calls == 1
    assert continuation.requests == []


def test_dependency_handoff_failure_keeps_the_failure_local_to_the_slot() -> None:
    generator = DependencyRaisingGenerator()
    continuation = Continuations(fail=True)

    slot = InsightSlotAssembler(Cache(), PromptsForEachSlot(), generator, 1, continuation).one(
        _request()
    )

    assert slot.state is InsightSlotState.UNAVAILABLE
    assert generator.calls == 1
    assert continuation.requests == []


def test_missing_activation_hands_nothing_off() -> None:
    """No prompt means no immutable generation request exists to recover."""
    continuation = Continuations()

    slot = InsightSlotAssembler(Cache(), Prompts(None), Generator(), 1, continuation).one(
        _request()
    )

    assert slot.state is InsightSlotState.UNAVAILABLE
    assert continuation.requests == []


def _metric_focus_requests(evidence: InsightEvidenceBundle) -> tuple[InsightAssemblyRequest, ...]:
    contracts = {
        contract.id: contract for _, contract in load_insight_contracts(Path(__file__).parents[2])
    }
    return declared_insight_requests(
        DeclaredInsightRequestInput(
            context=InsightRequestContext(
                "analytical-scope-v1:scope-fictional",
                "campaign-fictional:metric-focus:spend",
                DateRange(date(2026, 8, 1), date(2026, 8, 7)),
                "sha256:artifact-fictional",
            ),
            contracts=contracts,
            findings_by_slot={},
            slot_ids=(InsightSlotId.METRIC_FOCUS,),
            window_profile=_PROFILE,
            evidence_by_slot={InsightSlotId.METRIC_FOCUS: evidence},
        )
    ).requests


def test_material_metric_focus_evidence_reaches_the_generator() -> None:
    evidence = InsightEvidenceBundle(
        (InsightEvidenceInput.FOCUSED_METRIC,),
        (
            PredicateFinding(
                DeterministicFinding("spend", Decimal("400")),
                InsightFindingKind.ABSOLUTE_CHANGE,
                (InsightEvidenceInput.FOCUSED_METRIC,),
                FindingUnit.CURRENCY,
                OperandBasis.CAMPAIGN_WINDOW,
                "ad_spend",
            ),
            PredicateFinding(
                DeterministicFinding("spend", Decimal("20")),
                InsightFindingKind.PERCENTAGE_CHANGE,
                (InsightEvidenceInput.FOCUSED_METRIC,),
                FindingUnit.PERCENT,
                OperandBasis.CAMPAIGN_WINDOW,
                "ad_spend",
                MetricScale.CURRENCY,
            ),
        ),
    )
    requests = _metric_focus_requests(evidence)
    generator = Generator()

    slots = InsightSlotAssembler(Cache(), PromptsForEachSlot(), generator, 1).many(requests)

    assert len(requests) == 1
    assert len(slots) == 1
    assert slots[0].state.value in {"filled", "silent"}
    assert generator.calls == 1


@pytest.mark.parametrize(
    "evidence",
    (
        InsightEvidenceBundle((), ()),
        InsightEvidenceBundle(
            (InsightEvidenceInput.FOCUSED_METRIC,),
            (
                PredicateFinding(
                    DeterministicFinding("spend", Decimal("0.001")),
                    InsightFindingKind.ABSOLUTE_CHANGE,
                    (InsightEvidenceInput.FOCUSED_METRIC,),
                    FindingUnit.CURRENCY,
                    OperandBasis.CAMPAIGN_WINDOW,
                    "ad_spend",
                ),
                PredicateFinding(
                    DeterministicFinding("spend", Decimal("0.001")),
                    InsightFindingKind.PERCENTAGE_CHANGE,
                    (InsightEvidenceInput.FOCUSED_METRIC,),
                    FindingUnit.PERCENT,
                    OperandBasis.CAMPAIGN_WINDOW,
                    "ad_spend",
                    MetricScale.CURRENCY,
                ),
            ),
        ),
    ),
)
def test_contract_silenced_metric_focus_never_calls_the_generator(
    evidence: InsightEvidenceBundle,
) -> None:
    requests = _metric_focus_requests(evidence)
    generator = Generator()

    slots = InsightSlotAssembler(Cache(), PromptsForEachSlot(), generator, 1).many(requests)

    assert requests == ()
    assert slots == ()
    assert generator.calls == 0


def test_insight_text_and_evidence_enforce_encoded_byte_bounds() -> None:
    with pytest.raises(ValueError, match="narration exceeds"):
        InsightGeneratedNarration(
            InsightOutputClass.PERSPECTIVE,
            ("\0" * MAX_INSIGHT_SLOT_TEXT_BYTES,),
        )
    with pytest.raises(ValueError, match="byte bound"):
        InsightSlot(
            InsightSlotId.CONTRIBUTION,
            InsightOutputClass.PERSPECTIVE,
            InsightSlotState.FILLED,
            "\0" * MAX_INSIGHT_SLOT_TEXT_BYTES,
            "Evidence.",
            "revision",
            "v1",
        )
    with pytest.raises(ValueError, match="evidence line"):
        InsightSlot(
            InsightSlotId.CONTRIBUTION,
            InsightOutputClass.PERSPECTIVE,
            InsightSlotState.FILLED,
            "Bounded insight.",
            "\0" * MAX_INSIGHT_EVIDENCE_BYTES,
            "revision",
            "v1",
        )


def test_assembly_regenerates_a_cached_degraded_placeholder_and_retains_the_fill() -> None:
    """A retry-exhausted unavailable row is a placeholder, never a tombstone.

    Without regeneration a transient failure that exhausted its receives would
    freeze the slot unavailable for the life of its revision (ADR-068 amendment).
    """
    request = _request()
    cache = Cache()
    cache.values[request.key] = InsightSlot(
        id=request.key.slot_id,
        output_class=InsightOutputClass.PERSPECTIVE,
        state=InsightSlotState.UNAVAILABLE,
        text=None,
        evidence_line=None,
        artifact_revision=request.key.artifact_revision,
        prompt_version=request.key.prompt_version,
    )
    generator = Generator()
    assembler = InsightSlotAssembler(
        cache,
        Prompts(
            InsightPromptActivation(
                InsightSlotId.CONTRIBUTION, "v1", "Prompt.", InsightModelRoute.QUICK
            )
        ),
        generator,
        max_concurrency=1,
    )

    served = assembler.one(request)

    assert generator.calls == 1
    assert served.state is InsightSlotState.FILLED
    assert cache.values[request.key].state is InsightSlotState.FILLED


@dataclass
class DeepCache:
    records: dict[str, InsightDeepRecord] = field(default_factory=dict[str, InsightDeepRecord])

    def get_deep(self, key: InsightCacheKey) -> InsightDeepRecord | None:
        return self.records.get(key.content_key)

    def put_deep_if_absent(
        self, record: InsightDeepRecord, key: InsightCacheKey
    ) -> InsightDeepCacheWriteResult:
        winner = self.records.setdefault(key.content_key, record)
        return InsightDeepCacheWriteResult(winner is record, winner)


@dataclass
class DeepGenerator(_NoDeep):
    calls: list[InsightGenerationRequest] = field(default_factory=list[InsightGenerationRequest])

    def generate(self, request: InsightGenerationRequest) -> InsightGeneratedNarration:
        del request
        raise NotImplementedError("card generation is not exercised by this fake.")

    def generate_deep(self, request: InsightGenerationRequest) -> InsightDeepAnalysis:
        self.calls.append(request)
        return InsightDeepAnalysis(
            "A fictional movement of 5 stands out.",
            ("Reading one: the supplied movement may be temporary.",),
            "The next delivered window would discriminate.",
            ("5",),
        )


@pytest.mark.covers("ADR-076")
def test_deep_dive_resolves_the_deep_pointer_and_repeat_open_is_a_cache_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    deep_cache = DeepCache()
    generator = DeepGenerator()
    metrics: list[tuple[InsightPromptTier, InsightCacheOutcome]] = []

    def record_metric(tier: InsightPromptTier, outcome: InsightCacheOutcome) -> None:
        metrics.append((tier, outcome))

    monkeypatch.setattr(
        "adapters.inbound.presentation_handlers.insights.report_cache_outcome",
        record_metric,
    )
    assembler = InsightSlotAssembler(
        Cache(),
        Prompts(
            InsightPromptActivation(
                InsightSlotId.CONTRIBUTION,
                "v9",
                "A reviewed deep prompt.",
                InsightModelRoute.DEEP,
            )
        ),
        generator,
        1,
        deep_cache=deep_cache,
    )
    enrich_calls: list[str] = []

    def enrich(assembly: InsightAssemblyRequest) -> InsightAssemblyRequest:
        enrich_calls.append(assembly.key.content_key)
        return assembly

    first = assembler.deep_dive(request, correlation_id="correlation-fictional", enrich=enrich)
    second = assembler.deep_dive(request, correlation_id="correlation-fictional", enrich=enrich)

    assert first.state is InsightDeepDiveState.FILLED
    assert first.cache_outcome == "miss"
    assert first.prompt_version == "v9"
    assert second.state is InsightDeepDiveState.FILLED
    assert second.cache_outcome == "hit"
    assert len(generator.calls) == 1
    assert len(enrich_calls) == 1
    assert generator.calls[0].key.tier is InsightPromptTier.DEEP
    # The deep key carries the declared deep-input revision and the card's input digest.
    assert generator.calls[0].key.deep_input_revision == DEEP_INPUT_REVISION
    assert generator.calls[0].key.input_digest == request.key.input_digest
    assert generator.calls[0].prompt.model_route is InsightModelRoute.DEEP
    assert metrics == [
        (InsightPromptTier.DEEP, InsightCacheOutcome.MISS),
        (InsightPromptTier.DEEP, InsightCacheOutcome.HIT),
    ]


@pytest.mark.covers("ADR-076")
def test_deep_dive_defers_to_the_continuation_when_the_request_budget_is_short() -> None:
    """A short remaining envelope must never start a synchronous generation.

    The 2026-09-24 live defect: a capability-bound detail render consumed most of
    the Lambda budget, the fixed-timeout generation ran anyway, and the envelope
    killed the request into a bodyless 500. A short budget hands the enriched
    request to the durable continuation and serves pending instead.
    """
    request = _request()
    deep_cache = DeepCache()
    generator = DeepGenerator()
    continuations = Continuations()
    assembler = InsightSlotAssembler(
        Cache(),
        Prompts(
            InsightPromptActivation(
                InsightSlotId.CONTRIBUTION,
                "v9",
                "A reviewed deep prompt.",
                InsightModelRoute.DEEP,
            )
        ),
        generator,
        1,
        continuations,
        deep_cache=deep_cache,
        generation_timeout_seconds=20,
    )
    enrich_calls: list[str] = []

    def enrich(assembly: InsightAssemblyRequest) -> InsightAssemblyRequest:
        enrich_calls.append(assembly.key.content_key)
        return assembly

    # 12s remaining < 20s generation timeout + write headroom: defer, don't start.
    serve = assembler.deep_dive(
        request,
        correlation_id="correlation-fictional",
        enrich=enrich,
        remaining_budget_ms=lambda: 12_000,
    )

    assert serve.state is InsightDeepDiveState.PENDING
    assert generator.calls == []
    assert len(continuations.requests) == 1
    assert len(enrich_calls) == 1
    assert deep_cache.records == {}

    # A budget that absorbs the full timeout generates synchronously as before.
    filled = assembler.deep_dive(
        request,
        correlation_id="correlation-fictional",
        enrich=enrich,
        remaining_budget_ms=lambda: 25_000,
    )

    assert filled.state is InsightDeepDiveState.FILLED
    assert len(generator.calls) == 1
