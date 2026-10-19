# SPDX-License-Identifier: MIT-0
# ruff: noqa: PLR0913, PLR0917
"""Bounded parallel cache-or-generate assembly for declared presentation insight slots."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass, field, replace
from queue import SimpleQueue
from threading import Lock
from time import monotonic

from aws_lambda_powertools import Logger

from adapters.deep_input_revision import DEEP_INPUT_REVISION
from adapters.outbound.telemetry.reporting import (
    LogSeverity,
    OperationLog,
    Report,
    report_cache_outcome,
    report_error,
    report_operation,
)
from domain.presentation.insights import (
    DeterministicFinding,
    InsightAbsenceClass,
    InsightCacheKey,
    InsightCacheOutcome,
    InsightContextEntry,
    InsightContinuationRequest,
    InsightDeepAnalysis,
    InsightDeepDiveState,
    InsightDeepRecord,
    InsightGeneratedNarration,
    InsightGenerationRequest,
    InsightNextStepOption,
    InsightOutputClass,
    InsightPromptActivation,
    InsightPromptTier,
    InsightSlot,
    InsightSlotId,
    InsightSlotState,
    InsightWindowProfile,
    unavailable_insight_slots,
)
from domain.shared_kernel.errors import DependencyError, InvariantError
from ports.outbound.insights import (
    InsightCachePort,
    InsightContinuationPort,
    InsightDeepCachePort,
    InsightGenerationPort,
    InsightPromptActivationPort,
)

LOGGER = Logger(service="adlytics-presentation-insights")
_OPERATION = "presentation.insight_generation"
_COMPONENT = "presentation.insight_assembler"
# The log field makes the tier a queryable fact beside model_route; the deep-dive
# request path serves the deep tier (ADR-076 slice G).
_CARD_TIER = "card"
# A synchronous deep generation attempt is started only when the remaining request
# budget can absorb a full generation timeout plus response-write headroom; a
# shorter budget defers straight to the durable continuation instead of letting
# the Lambda envelope kill the request mid-generation (the 2026-09-24 bodyless-500
# class on capability-bound detail serves).
_DEEP_BUDGET_HEADROOM_MS = 2_000

# Governed customer-legible deep-section reasons (contract §3.1): the stored degrade
# row keeps its diagnostic code; the served reason is always one of these.
_DEEP_PENDING_REASON = (
    "The deep analysis is still being prepared. It completes automatically; "
    "reopening this view shows it once ready."
)
_DEEP_UNAVAILABLE_REASON = (
    "The deep analysis could not be prepared for this view right now. "
    "The insight above remains exact."
)
_DEEP_NOT_ENABLED_REASON = (
    "Deep analysis is not enabled for this insight yet. The insight above remains exact."
)


@dataclass(frozen=True, slots=True)
class InsightAssemblyRequest:
    """One model-authored declared slot with server-owned findings and cache identity.

    ``silence_reason`` is the contract's governed silence condition: the exact
    customer-legible sentence a slot carries when the model evaluates the findings
    and declines to narrate, so an evaluated-quiet slot never renders unexplained.
    """

    key: InsightCacheKey
    output_class: InsightOutputClass
    purpose: str
    findings: tuple[DeterministicFinding, ...]
    silence_reason: str
    window_profile: InsightWindowProfile
    context: tuple[InsightContextEntry, ...] = ()
    next_step_options: tuple[InsightNextStepOption, ...] = ()
    """FR-11: the served steps the card's model may choose from; empty while dormant."""

    def __post_init__(self) -> None:
        if self.key.slot_id is InsightSlotId.MOVERS or not self.purpose.strip():
            raise ValueError("model insight assembly cannot be used for movers or blank purpose.")
        if not self.silence_reason.strip():
            raise ValueError("model insight assembly requires the contract's silence condition.")


@dataclass(frozen=True, slots=True)
class InsightDeepServe:
    """One served deep section: the §3.1 state plus provenance and telemetry facts.

    ``absence_reason`` is always one of the governed customer-legible reasons —
    never a stored diagnostic code — and is present exactly when the state is not
    filled. ``prompt_version`` is the deep activation's version when one resolved.
    """

    state: InsightDeepDiveState
    analysis: InsightDeepAnalysis | None
    absence_reason: str | None
    prompt_version: str
    cache_outcome: str
    provider_ms: int


@dataclass(frozen=True, slots=True)
class _SlotObservation:
    """Bounded non-content timing and outcome evidence for one declared slot."""

    slot_id: InsightSlotId
    slot_state: InsightSlotState
    outcome: str
    cache_outcome: str
    generated: bool
    provider_ms: int
    start_offset_ms: int
    end_offset_ms: int


@dataclass(slots=True)
class _ModelConcurrency:
    """Thread-safe observed occupancy of actual model calls in one assembly batch."""

    lock: Lock = field(default_factory=Lock)
    active: int = 0
    maximum: int = 0

    def enter(self) -> None:
        with self.lock:
            self.active += 1
            self.maximum = max(self.maximum, self.active)

    def leave(self) -> None:
        with self.lock:
            self.active -= 1


@dataclass(frozen=True, slots=True)
class _ObservedAssemblyRequest:
    """One slot request plus its request-owned telemetry context."""

    assembly: InsightAssemblyRequest
    correlation_id: str
    batch_started_at: float
    observations: SimpleQueue[_SlotObservation] | None = None
    model_concurrency: _ModelConcurrency | None = None


@dataclass(slots=True)
class _MutableObservation:
    """One thread-owned observation assembled across cache and provider phases."""

    cache_outcome: str = "not_read"
    generated: bool = False
    provider_ms: int = 0
    prompt_version: str = "unresolved"
    model_route: str = "unresolved"
    fragment_digest: str = "unresolved"
    recovery_admitted: bool = False
    outcome: str = "unavailable"


def _safe_report_error(error: Exception, correlation_id: str) -> None:
    """Keep observability failure from changing an independently completed slot."""
    try:
        report_error(
            error,
            Report(operation=_OPERATION, correlation_id=correlation_id, logger=LOGGER),
        )
    # Telemetry is never a serving dependency.
    # error-propagation: guards the telemetry seam itself; a failed report cannot be reported
    except Exception:
        return


def _safe_report_operation(event: OperationLog) -> None:
    """Keep lifecycle export failure outside the customer result boundary."""
    try:
        report_operation(event, LOGGER)
    # Telemetry is never a serving dependency.
    # error-propagation: guards the telemetry seam itself; a failed report cannot be reported
    except Exception:
        return


def _safe_report_cache_outcome(tier: InsightPromptTier, outcome: str) -> None:
    """Keep metric emission failure outside the customer result boundary."""
    try:
        report_cache_outcome(tier, InsightCacheOutcome(outcome))
    # Telemetry is never a serving dependency.
    # error-propagation: guards the telemetry seam itself; a failed report cannot be reported
    except Exception:
        return


class InsightSlotAssembler:
    """Contain each slot failure while joining independent requests in bounded parallel."""

    def __init__(
        self,
        cache: InsightCachePort,
        prompts: InsightPromptActivationPort,
        generator: InsightGenerationPort,
        max_concurrency: int,
        continuation: InsightContinuationPort | None = None,
        *,
        deep_cache: InsightDeepCachePort | None = None,
        generation_timeout_seconds: int | None = None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("insight assembly concurrency must be positive.")
        self._cache = cache
        self._prompts = prompts
        self._generator = generator
        self._max_concurrency = max_concurrency
        self._continuation = continuation
        self._deep_cache = deep_cache
        self._generation_timeout_seconds = generation_timeout_seconds
        self._clock = clock

    def one(self, request: InsightAssemblyRequest) -> InsightSlot:
        """Resolve one slot without an application deadline; failures remain slot-local."""
        started = self._clock()
        return self._observed_one(_ObservedAssemblyRequest(request, "", started))

    def _observed_one(self, request: _ObservedAssemblyRequest) -> InsightSlot:
        assembly = request.assembly
        started = self._clock()
        observation = _MutableObservation()
        generation: InsightGenerationRequest | None = None
        slot = self._unavailable(assembly)
        error: Exception | None = None
        try:
            # Everything this composer fills is the card tier (the deep tier's serving
            # path is slice G), so the card pointer is the one resolved here.
            prompt = self._prompts.active(assembly.key.slot_id, InsightPromptTier.CARD)
            if prompt is None:
                observation.outcome = "prompt_unavailable"
                return slot
            observation.prompt_version = prompt.version
            observation.model_route = prompt.model_route.value
            # Composed versions stamp their combined source digest (ADR-076 decision 6);
            # direct, seeded, and pre-tier versions read as unsourced.
            observation.fragment_digest = prompt.fragment_digest or "unsourced"
            generation = self._generation(assembly, prompt)
            cached = self._cache.get(generation.key)
            if cached is not None and cached.state is not InsightSlotState.UNAVAILABLE:
                observation.cache_outcome = "hit"
                observation.outcome = cached.state.value
                slot = self._declared_absence(cached, assembly.silence_reason)
                return slot
            observation.cache_outcome = "degraded_hit" if cached is not None else "miss"
            observation.generated = True
            generated = self._generated_narration(
                generation, observation, request.model_concurrency
            )
            slot = self._declared_absence(
                self._cache.put_if_absent(generated, generation.key).slot,
                assembly.silence_reason,
            )
            observation.outcome = slot.state.value
            return slot
        except DependencyError as caught:
            error = caught
            observation.outcome = "dependency_failed"
            if generation is not None:
                observation.recovery_admitted = self._handoff(generation, request.correlation_id)
            return slot
        except Exception as caught:
            error = caught
            observation.outcome = (
                "invariant_failed" if isinstance(caught, InvariantError) else "failed"
            )
            return slot
        finally:
            ended = self._clock()
            terminal = _SlotObservation(
                slot_id=assembly.key.slot_id,
                slot_state=slot.state,
                outcome=observation.outcome,
                cache_outcome=observation.cache_outcome,
                generated=observation.generated,
                provider_ms=observation.provider_ms,
                start_offset_ms=int((started - request.batch_started_at) * 1_000),
                end_offset_ms=int((ended - request.batch_started_at) * 1_000),
            )
            # Publish the join evidence before optional telemetry. A logger/exporter failure
            # can never shorten the queue or strand `many()` after business work completed.
            if request.observations is not None:
                request.observations.put(terminal)
            # The owner-deliverable cache metric (ADR-076 slice G): emitted only when
            # the cache was actually read, dimensioned on the closed enums alone.
            if terminal.cache_outcome != "not_read":
                _safe_report_cache_outcome(InsightPromptTier.CARD, terminal.cache_outcome)
            if error is not None:
                _safe_report_error(error, request.correlation_id)
            _safe_report_operation(
                OperationLog(
                    operation=_OPERATION,
                    component=_COMPONENT,
                    phase="slot",
                    outcome=terminal.outcome,
                    severity=(
                        LogSeverity.INFO
                        if terminal.slot_state is not InsightSlotState.UNAVAILABLE
                        else LogSeverity.WARNING
                    ),
                    details={
                        "correlation_id": request.correlation_id,
                        "slot": terminal.slot_id.value,
                        "prompt_version": observation.prompt_version,
                        "model_route": observation.model_route,
                        "fragment_digest": observation.fragment_digest,
                        "tier": _CARD_TIER,
                        "horizon_role": assembly.window_profile.role.value,
                        "cache_outcome": terminal.cache_outcome,
                        "generated": terminal.generated,
                        "provider_ms": terminal.provider_ms,
                        "total_ms": terminal.end_offset_ms - terminal.start_offset_ms,
                        "start_offset_ms": terminal.start_offset_ms,
                        "end_offset_ms": terminal.end_offset_ms,
                        "finding_count": len(assembly.findings),
                        "context_count": len(assembly.context),
                        "recovery_admitted": observation.recovery_admitted,
                    },
                )
            )

    def _generated_narration(
        self,
        generation: InsightGenerationRequest,
        observation: _MutableObservation,
        concurrency: _ModelConcurrency | None,
    ) -> InsightSlot:
        """Generate one narration while stamping provider timing and actual occupancy."""
        provider_started = self._clock()
        if concurrency is not None:
            concurrency.enter()
        try:
            narration = self._generator.generate(generation)
        finally:
            observation.provider_ms = int((self._clock() - provider_started) * 1_000)
            if concurrency is not None:
                concurrency.leave()
        return self._slot(generation, narration)

    def _handoff(self, generation: InsightGenerationRequest, correlation_id: str) -> bool:
        """Durably admit an exact request the serve path could not fill synchronously.

        Two admitters: a classified dependency failure, and a deep serve whose
        remaining request budget cannot absorb a full generation timeout.
        """
        if self._continuation is None:
            return False
        try:
            self._continuation.enqueue(InsightContinuationRequest(generation))
        except Exception as error:
            _safe_report_error(error, correlation_id)
            return False
        return True

    def fill(self, generation: InsightGenerationRequest) -> InsightSlot:
        """Fill one retained exact request without resolving the active pointer again."""
        return self._one_generation(generation)

    def fill_deep(self, generation: InsightGenerationRequest) -> InsightDeepRecord:
        """Fill one exact immutable deep key; a degrade row heals exactly like the card tier."""
        if self._deep_cache is None:
            raise RuntimeError("deep cache runtime is unavailable for this fill.")
        cached = self._deep_cache.get_deep(generation.key)
        if cached is not None and cached.state is InsightDeepDiveState.FILLED:
            return cached
        analysis = self._generator.generate_deep(generation)
        record = InsightDeepRecord(
            slot_id=generation.key.slot_id,
            state=InsightDeepDiveState.FILLED,
            analysis=analysis,
            absence_reason=None,
            artifact_revision=generation.key.artifact_revision,
            prompt_version=generation.key.prompt_version,
        )
        return self._deep_cache.put_deep_if_absent(record, generation.key).record

    def deep_dive(  # noqa: PLR0915
        self,
        assembly: InsightAssemblyRequest,
        *,
        correlation_id: str = "",
        enrich: Callable[[InsightAssemblyRequest], InsightAssemblyRequest] | None = None,
        remaining_budget_ms: Callable[[], int] | None = None,
    ) -> InsightDeepServe:
        """Serve one deep section: cache hit, synchronous deep fill, or pending handoff.

        Mirrors the card path's containment exactly: a dependency failure enqueues
        the continuation and serves the pending treatment; an invariant failure
        serves the quiet designed absence; the card content above is never blocked
        (contract §3.1). ``enrich`` supplies the deep tier's extended payload
        (series findings, sibling direction context) and never runs on a cache hit,
        so a repeat open pays no sibling artifact reads; it does run before the
        budget guard, because a deferring request hands its exact enriched payload
        to the continuation. ``remaining_budget_ms`` (the Lambda envelope's
        remaining-time reader) defers the synchronous generation to the durable
        continuation when a full generation timeout no longer fits. The DEBUG start
        event plus the terminal slot log make one dialog open traceable end to end
        (owner scope amendment).
        """
        started = self._clock()
        observation = _MutableObservation()
        serve = InsightDeepServe(
            state=InsightDeepDiveState.UNAVAILABLE,
            analysis=None,
            absence_reason=_DEEP_NOT_ENABLED_REASON,
            prompt_version="not-activated",
            cache_outcome="not_read",
            provider_ms=0,
        )
        error: Exception | None = None
        generation: InsightGenerationRequest | None = None
        _safe_report_operation(
            OperationLog(
                operation=_OPERATION,
                component=_COMPONENT,
                phase="start",
                outcome="deep_requested",
                severity=LogSeverity.DEBUG,
                details={
                    "correlation_id": correlation_id,
                    "slot": assembly.key.slot_id.value,
                    "tier": InsightPromptTier.DEEP.value,
                },
            )
        )
        try:
            prompt = self._prompts.active(assembly.key.slot_id, InsightPromptTier.DEEP)
            if prompt is None or self._deep_cache is None:
                observation.outcome = "prompt_unavailable"
                return serve
            observation.prompt_version = prompt.version
            observation.model_route = prompt.model_route.value
            observation.fragment_digest = prompt.fragment_digest or "unsourced"
            key = replace(
                assembly.key,
                tier=InsightPromptTier.DEEP,
                prompt_version=prompt.version,
                deep_input_revision=DEEP_INPUT_REVISION,
            )
            cached = self._deep_cache.get_deep(key)
            if cached is not None and cached.state is InsightDeepDiveState.FILLED:
                observation.cache_outcome = "hit"
                observation.outcome = "filled"
                serve = InsightDeepServe(
                    InsightDeepDiveState.FILLED,
                    cached.analysis,
                    None,
                    prompt.version,
                    "hit",
                    0,
                )
                return serve
            observation.cache_outcome = "degraded_hit" if cached is not None else "miss"
            if enrich is not None:
                assembly = enrich(assembly)
            generation = InsightGenerationRequest(
                key,
                assembly.output_class,
                prompt,
                assembly.purpose,
                assembly.findings,
                assembly.window_profile,
                assembly.context,
            )
            if (
                remaining_budget_ms is not None
                and self._generation_timeout_seconds is not None
                and remaining_budget_ms()
                < self._generation_timeout_seconds * 1_000 + _DEEP_BUDGET_HEADROOM_MS
            ):
                # The budget is read HERE, after enrich: sibling reads spend real
                # seconds, and a pre-enrich snapshot could approve a generation the
                # envelope can no longer finish. The enrichment cost is deliberately
                # paid even on a deferring request — the continuation must carry the
                # exact enriched payload a synchronous attempt would have used. A
                # deferring open can enqueue once per dialog poll; the worker's
                # cache check converges the duplicates on the first filled row.
                observation.outcome = "budget_deferred"
                observation.recovery_admitted = self._handoff(generation, correlation_id)
                serve = InsightDeepServe(
                    InsightDeepDiveState.PENDING
                    if observation.recovery_admitted
                    else InsightDeepDiveState.UNAVAILABLE,
                    None,
                    _DEEP_PENDING_REASON
                    if observation.recovery_admitted
                    else _DEEP_UNAVAILABLE_REASON,
                    prompt.version,
                    observation.cache_outcome,
                    0,
                )
                return serve
            observation.generated = True
            provider_started = self._clock()
            try:
                analysis = self._generator.generate_deep(generation)
            finally:
                observation.provider_ms = int((self._clock() - provider_started) * 1_000)
            stored = self._deep_cache.put_deep_if_absent(
                InsightDeepRecord(
                    slot_id=key.slot_id,
                    state=InsightDeepDiveState.FILLED,
                    analysis=analysis,
                    absence_reason=None,
                    artifact_revision=key.artifact_revision,
                    prompt_version=key.prompt_version,
                ),
                key,
            ).record
            observation.outcome = "filled"
            serve = InsightDeepServe(
                InsightDeepDiveState.FILLED,
                stored.analysis,
                None,
                prompt.version,
                observation.cache_outcome,
                observation.provider_ms,
            )
            return serve
        except DependencyError as caught:
            error = caught
            observation.outcome = "dependency_failed"
            if generation is not None:
                observation.recovery_admitted = self._handoff(generation, correlation_id)
            serve = InsightDeepServe(
                InsightDeepDiveState.PENDING
                if observation.recovery_admitted
                else InsightDeepDiveState.UNAVAILABLE,
                None,
                _DEEP_PENDING_REASON if observation.recovery_admitted else _DEEP_UNAVAILABLE_REASON,
                observation.prompt_version,
                observation.cache_outcome,
                observation.provider_ms,
            )
            return serve
        except Exception as caught:
            error = caught
            observation.outcome = (
                "invariant_failed" if isinstance(caught, InvariantError) else "failed"
            )
            serve = InsightDeepServe(
                InsightDeepDiveState.UNAVAILABLE,
                None,
                _DEEP_UNAVAILABLE_REASON,
                observation.prompt_version,
                observation.cache_outcome,
                observation.provider_ms,
            )
            return serve
        finally:
            ended = self._clock()
            if observation.cache_outcome != "not_read":
                _safe_report_cache_outcome(InsightPromptTier.DEEP, observation.cache_outcome)
            if error is not None:
                _safe_report_error(error, correlation_id)
            _safe_report_operation(
                OperationLog(
                    operation=_OPERATION,
                    component=_COMPONENT,
                    phase="slot",
                    outcome=observation.outcome,
                    severity=(
                        LogSeverity.INFO
                        if serve.state is not InsightDeepDiveState.UNAVAILABLE
                        else LogSeverity.WARNING
                    ),
                    details={
                        "correlation_id": correlation_id,
                        "slot": assembly.key.slot_id.value,
                        "prompt_version": observation.prompt_version,
                        "model_route": observation.model_route,
                        "fragment_digest": observation.fragment_digest,
                        "tier": InsightPromptTier.DEEP.value,
                        "horizon_role": assembly.window_profile.role.value,
                        "cache_outcome": observation.cache_outcome,
                        "generated": observation.generated,
                        "provider_ms": observation.provider_ms,
                        "total_ms": int((ended - started) * 1_000),
                        "finding_count": len(assembly.findings),
                        "context_count": len(assembly.context),
                        "recovery_admitted": observation.recovery_admitted,
                    },
                )
            )

    def _one_generation(self, generation: InsightGenerationRequest) -> InsightSlot:
        """Fill one exact immutable key and deterministically retain its cache winner.

        A cached retry-exhausted ``unavailable`` row is a placeholder, never a tombstone:
        it is regenerated here, and the conditional cache write replaces it only while it
        is still degraded, so a transient failure heals without clobbering a racing winner.
        """
        cached = self._cache.get(generation.key)
        if cached is not None and cached.state is not InsightSlotState.UNAVAILABLE:
            return cached
        narration = self._generator.generate(generation)
        generated = self._slot(generation, narration)
        return self._cache.put_if_absent(generated, generation.key).slot

    def many(
        self,
        requests: tuple[InsightAssemblyRequest, ...],
        *,
        correlation_id: str = "",
    ) -> tuple[InsightSlot, ...]:
        """Run every admitted slot concurrently and wait for the bounded roster to settle."""
        if not requests:
            return ()
        started = self._clock()
        observations: SimpleQueue[_SlotObservation] = SimpleQueue()
        model_concurrency = _ModelConcurrency()
        with ThreadPoolExecutor(max_workers=min(self._max_concurrency, len(requests))) as executor:
            pending: tuple[Future[InsightSlot], ...] = tuple(
                executor.submit(
                    copy_context().run,
                    self._observed_one,
                    _ObservedAssemblyRequest(
                        request,
                        correlation_id,
                        started,
                        observations,
                        model_concurrency,
                    ),
                )
                for request in requests
            )
            results = tuple(
                self._future_slot(future, request)
                for future, request in zip(pending, requests, strict=True)
            )
        ended = self._clock()
        terminals = tuple(observations.get() for _ in requests)
        failed = sum(item.slot_state is InsightSlotState.UNAVAILABLE for item in terminals)
        _safe_report_operation(
            OperationLog(
                operation=_OPERATION,
                component=_COMPONENT,
                phase="batch",
                outcome="completed" if failed == 0 else "degraded",
                severity=LogSeverity.INFO if failed == 0 else LogSeverity.WARNING,
                details={
                    "correlation_id": correlation_id,
                    "tier": _CARD_TIER,
                    # One batch serves one window, so every admitted request shares a role.
                    "horizon_role": requests[0].window_profile.role.value,
                    "admitted_count": len(requests),
                    "cache_hit_count": sum(item.cache_outcome == "hit" for item in terminals),
                    "generated_count": sum(item.generated for item in terminals),
                    "failed_count": failed,
                    "configured_concurrency": self._max_concurrency,
                    "maximum_model_concurrency": model_concurrency.maximum,
                    "batch_ms": int((ended - started) * 1_000),
                },
            )
        )
        return results

    @staticmethod
    def _future_slot(future: Future[InsightSlot], request: InsightAssemblyRequest) -> InsightSlot:
        try:
            return future.result()
        except Exception as error:
            _safe_report_error(error, "")
            return InsightSlotAssembler._unavailable(request)

    @staticmethod
    def _generation(
        request: InsightAssemblyRequest, prompt: InsightPromptActivation
    ) -> InsightGenerationRequest:
        key = replace(request.key, prompt_version=prompt.version)
        return InsightGenerationRequest(
            key,
            request.output_class,
            prompt,
            request.purpose,
            request.findings,
            request.window_profile,
            request.context,
            request.next_step_options,
        )

    @staticmethod
    def _slot(
        generation: InsightGenerationRequest, narration: InsightGeneratedNarration
    ) -> InsightSlot:
        if narration.text is None:
            return InsightSlot(
                generation.key.slot_id,
                generation.output_class,
                InsightSlotState.SILENT,
                None,
                None,
                generation.key.artifact_revision,
                generation.key.prompt_version,
            )
        return InsightSlot(
            generation.key.slot_id,
            generation.output_class,
            InsightSlotState.FILLED,
            narration.text,
            # Customer words only (product decision 2026-09-25): the served evidence
            # line names where the reading came from in the reader's vocabulary.
            "Read from the values this page already shows.",
            generation.key.artifact_revision,
            generation.key.prompt_version,
            next_step=narration.next_step,
        )

    @staticmethod
    def _declared_absence(slot: InsightSlot, silence_reason: str) -> InsightSlot:
        """Overlay the declared absence on a silent slot the cache round-trip stripped.

        The cache row keeps the seven-field ops shape; the serve path owns the customer
        taxonomy, so every silent slot it returns names the contract's silence condition.
        """
        if slot.state is not InsightSlotState.SILENT or slot.absence_class is not None:
            return slot
        return replace(
            slot,
            absence_class=InsightAbsenceClass.EVALUATED_QUIET,
            absence_reason=silence_reason,
        )

    @staticmethod
    def _unavailable(request: InsightAssemblyRequest) -> InsightSlot:
        return unavailable_insight_slots(
            (request.key.slot_id,),
            artifact_revision=request.key.artifact_revision,
            absence_class=InsightAbsenceClass.GENERATION_PENDING,
        )[0]
