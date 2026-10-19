# SPDX-License-Identifier: MIT-0
"""SQS worker that completes exact durable insight cache misses."""

from __future__ import annotations

from dataclasses import dataclass, replace
from functools import lru_cache
from typing import Any, Protocol, cast

from aws_lambda_powertools import Logger

from adapters.config import insight_continuation_worker_settings, tracing_settings
from adapters.inbound.presentation_handlers.insights import InsightSlotAssembler
from adapters.outbound.bedrock.insights import BedrockInsightGenerator, InsightModelRouteModels
from adapters.outbound.dynamodb.insight_cache import (
    DynamoInsightCache,
    InsightCacheDegradeRequest,
)
from adapters.outbound.dynamodb.insight_prompt_registry import DynamoInsightPromptRegistry
from adapters.outbound.sqs.insight_continuation import parse
from adapters.outbound.telemetry.bootstrap import configure_tracing, flushes_traces
from adapters.outbound.telemetry.reporting import (
    LogSeverity,
    OperationLog,
    Report,
    flushes_metrics,
    report_error,
    report_operation,
)
from adapters.outbound.telemetry.tracing import (
    INSIGHT_CONTINUATION,
    span,
    trace_boundary,
)
from domain.presentation.insights import (
    InsightDeepDiveState,
    InsightDeepRecord,
    InsightGenerationRequest,
    InsightPromptTier,
)
from domain.shared_kernel.errors import AdlyticsError

_TRACING_SETTINGS = tracing_settings()
_TRACING = configure_tracing(_TRACING_SETTINGS)
LOGGER = Logger(service="adlytics-insight-continuation")
_OPERATION = INSIGHT_CONTINUATION
_COMPONENT = "insight_continuation"

report_operation(
    OperationLog(
        operation=_OPERATION,
        component=_COMPONENT,
        phase="start",
        outcome="active" if _TRACING else "disabled",
        severity=LogSeverity.INFO,
        details={
            "stage": "startup",
            "tracing": _TRACING,
            "sample_ratio": _TRACING_SETTINGS.sample_ratio,
        },
    ),
    LOGGER,
)


class ContinuationApplication(Protocol):
    """Complete one already-admitted durable continuation or request its retry."""

    def apply(self, body: object, *, final_receive: bool = False) -> None:
        """Fill the immutable cache key or raise for SQS retry/DLQ handling."""
        ...


@dataclass(frozen=True, slots=True)
class InsightContinuationApplication:
    """Read immutable prompt identity and converge duplicates at the cache write boundary."""

    prompts: DynamoInsightPromptRegistry
    assembler: InsightSlotAssembler
    cache: DynamoInsightCache

    def apply(self, body: object, *, final_receive: bool = False) -> None:
        """Complete an admitted request with its immutable prompt, not the active pointer.

        On the final SQS receive a failing generation degrades to a durable
        ``unavailable`` cache row with a bounded reason instead of dead-lettering
        silently: the absence becomes visible in the served payload and the DLQ
        stays a truthful alarm surface for unexpected classes only.
        """
        continuation = parse(body)
        if continuation is None:
            return
        generation = continuation.generation
        prompt = self.prompts.version(generation.key.slot_id, generation.key.prompt_version)
        if prompt is None:
            # A missing immutable prompt version is permanent for this exact key; a
            # silent acknowledgement here was the last surviving instance of the
            # RCA's invisibility class. The degrade row makes the absence durable
            # and diagnosable without waiting for retry exhaustion.
            self._degrade(generation, reason="prompt_version_unavailable")
            return
        try:
            if generation.key.tier is InsightPromptTier.DEEP:
                self.assembler.fill_deep(replace(generation, prompt=prompt))
            else:
                self.assembler.fill(replace(generation, prompt=prompt))
        except Exception as error:
            if not final_receive:
                raise
            self._degrade(generation, error=error)

    def _degrade(
        self,
        generation: InsightGenerationRequest,
        *,
        error: Exception | None = None,
        reason: str | None = None,
    ) -> None:
        """Write the create-only degrade row; its own failure still reaches the DLQ."""
        if reason is None:
            reason = (
                str(error.code)
                if isinstance(error, AdlyticsError)
                else f"untyped_{type(error).__name__}"
            )
        if generation.key.tier is InsightPromptTier.DEEP:
            # The deep tier's placeholder is its own record shape; the stored reason
            # stays a diagnostic code and the serve path overlays the governed
            # customer-legible wording, exactly as the card tier does.
            deep_result = self.cache.put_deep_if_absent(
                InsightDeepRecord(
                    slot_id=generation.key.slot_id,
                    state=InsightDeepDiveState.UNAVAILABLE,
                    analysis=None,
                    absence_reason=reason,
                    artifact_revision=generation.key.artifact_revision,
                    prompt_version=generation.key.prompt_version,
                ),
                generation.key,
            )
            stored = deep_result.stored
        else:
            stored = self.cache.put_unavailable_if_absent(
                InsightCacheDegradeRequest(
                    key=generation.key,
                    output_class=generation.output_class,
                    reason=reason,
                )
            ).stored
        report_operation(
            OperationLog(
                operation=_OPERATION,
                component=_COMPONENT,
                phase="terminal",
                outcome="degraded",
                severity=LogSeverity.WARNING,
                details={
                    "stage": "retry_exhausted_degrade",
                    "slot": generation.key.slot_id.value,
                    "tier": generation.key.tier.value,
                    "reason": reason,
                    "stored": stored,
                },
            ),
            LOGGER,
        )


def _report_failure(error: BaseException) -> None:
    report_error(error, Report(operation=_OPERATION, correlation_id="", logger=LOGGER))


def _receive_count(record: dict[str, object]) -> int:
    """Read the positive delivery attempt count SQS stamps on every record."""
    attributes = record.get("attributes")
    if not isinstance(attributes, dict):
        return 0
    raw = cast("dict[str, object]", attributes).get("ApproximateReceiveCount")
    if not isinstance(raw, str):
        return 0
    try:
        count = int(raw)
    except ValueError:
        return 0
    return count if count > 0 else 0


def apply_records(
    event: object, application: ContinuationApplication, max_receive_count: int
) -> tuple[str, ...]:
    """Use partial-batch retry only for admitted work that cannot yet complete."""
    if not isinstance(event, dict):
        return ()
    records = cast("dict[str, object]", event).get("Records")
    if not isinstance(records, list):
        return ()
    failures: list[str] = []
    for raw in cast("list[object]", records):
        if not isinstance(raw, dict):
            continue
        record = cast("dict[str, object]", raw)
        try:
            application.apply(
                record.get("body"),
                final_receive=_receive_count(record) >= max_receive_count,
            )
        except Exception as error:
            _report_failure(error)
            message_id = record.get("messageId")
            if isinstance(message_id, str) and message_id:
                failures.append(message_id)
    return tuple(failures)


@lru_cache(maxsize=1)
def application() -> InsightContinuationApplication:
    """Compose only the queue worker's registry, cache, and Bedrock authority."""
    settings = insight_continuation_worker_settings()
    cache = DynamoInsightCache(settings.insight_cache_table)
    prompts = DynamoInsightPromptRegistry(settings.insight_prompt_registry_table)
    return InsightContinuationApplication(
        prompts=prompts,
        cache=cache,
        assembler=InsightSlotAssembler(
            cache=cache,
            prompts=prompts,
            generator=BedrockInsightGenerator(
                InsightModelRouteModels(
                    settings.insight_quick_model_id,
                    settings.insight_deep_model_id,
                ),
                settings.insight_generation_timeout_seconds,
            ),
            max_concurrency=1,
            deep_cache=cache,
        ),
    )


@trace_boundary(INSIGHT_CONTINUATION)
@flushes_traces
@flushes_metrics
@LOGGER.inject_lambda_context
def handler(event: dict[str, Any], _context: object) -> dict[str, list[dict[str, str]]]:
    """Return failed queue message identities so delivery is retried or reaches the DLQ."""
    with span(INSIGHT_CONTINUATION, operation=_OPERATION) as process_span:
        raw_records: object = event.get("Records", [])
        record_count = (
            len(cast("list[object]", raw_records)) if isinstance(raw_records, list) else 0
        )
        try:
            failures = apply_records(
                event,
                application(),
                insight_continuation_worker_settings().insight_continuation_max_receive_count,
            )
        except Exception as error:
            process_span.note_failure(error)
            process_span.record(
                outcome="failed",
                record_count=record_count,
                acknowledged_count=0,
                failed_count=record_count,
            )
            _report_failure(error)
            raise
        failed_count = len(failures)
        if failures:
            process_span.note_failure(RuntimeError("partial batch retry"))
        acknowledged_count = record_count - failed_count
        outcome = "completed" if not failures else "partial_retry"
        process_span.record(
            outcome=outcome,
            record_count=record_count,
            acknowledged_count=acknowledged_count,
            failed_count=failed_count,
        )
        report_operation(
            OperationLog(
                operation=_OPERATION,
                component=_COMPONENT,
                phase="terminal",
                outcome=outcome,
                severity=LogSeverity.INFO if not failures else LogSeverity.WARNING,
                details={
                    "record_count": record_count,
                    "acknowledged_count": acknowledged_count,
                    "failed_count": failed_count,
                },
            ),
            LOGGER,
        )
        return {"batchItemFailures": [{"itemIdentifier": item} for item in failures]}
