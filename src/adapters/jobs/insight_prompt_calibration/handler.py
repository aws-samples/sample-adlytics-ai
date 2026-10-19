# SPDX-License-Identifier: MIT-0
"""SQS worker that calibrates immutable prompt versions outside the HTTP deadline."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Protocol, cast
from uuid import uuid4

from aws_lambda_powertools import Logger

from adapters.config import insight_prompt_calibration_worker_settings, tracing_settings
from adapters.inbound.tool_handlers.gateway import contracts_root
from adapters.jobs.contract_validation.insight_loader import load_insight_contracts
from adapters.outbound.bedrock.insight_calibration import BedrockInsightCalibrationRunner
from adapters.outbound.bedrock.insights import InsightModelRouteModels
from adapters.outbound.dynamodb.insight_prompt_registry import DynamoInsightPromptRegistry
from adapters.outbound.sqs.insight_prompt_calibration import (
    SqsInsightPromptCalibrationQueue,
    parse,
)
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
    INSIGHT_PROMPT_CALIBRATION,
    span,
    trace_boundary,
)
from domain.presentation.insights import (
    InsightCalibrationClaimDisposition,
    InsightClaimedPromptCalibrationRequest,
    InsightContract,
    InsightPromptCalibrationClaimRequest,
    InsightPromptCalibrationRunRequest,
    InsightSlotId,
)
from ports.outbound.insights import (
    InsightPromptCalibrationQueuePort,
    InsightPromptCalibrationRunner,
    InsightPromptRegistryPort,
)

_TRACING_SETTINGS = tracing_settings()
_TRACING = configure_tracing(_TRACING_SETTINGS)
LOGGER = Logger(service="adlytics-insight-prompt-calibration")
_OPERATION = "insight_prompt_calibration.worker"
_COMPONENT = "insight_prompt_calibration"

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


class CalibrationApplication(Protocol):
    """Apply one at-least-once prompt calibration delivery."""

    def apply(self, body: object, *, lease_ref: str, receive_count: int) -> bool:
        """Return true to acknowledge terminal/completed work, false for active-lease retry."""
        ...


@dataclass(frozen=True, slots=True)
class InsightPromptCalibrationApplication:
    """Lease, calibrate, and complete one immutable pending version."""

    registry: InsightPromptRegistryPort
    runner: InsightPromptCalibrationRunner
    contracts: Mapping[InsightSlotId, InsightContract]
    lease_seconds: int
    wall: Callable[[], float] = time.time

    def apply(self, body: object, *, lease_ref: str, receive_count: int) -> bool:
        """Converge terminal duplicates and retry only unavailable or actively leased work."""
        work = parse(body)
        now = int(self.wall())
        claim = self.registry.claim_calibration(
            InsightPromptCalibrationClaimRequest(
                work.slot_id,
                work.version,
                lease_ref,
                now,
                now + self.lease_seconds,
            )
        )
        if claim.disposition is InsightCalibrationClaimDisposition.TERMINAL:
            return True
        if claim.disposition is InsightCalibrationClaimDisposition.ACTIVE_LEASE:
            return False
        pending = claim.pending
        contract = self.contracts.get(work.slot_id)
        if pending is None or contract is None:
            raise ValueError("claimed prompt calibration has no declared contract.")
        result = self.runner.run(
            InsightPromptCalibrationRunRequest(
                pending.slot_id,
                pending.version,
                pending.prompt,
                pending.model_route,
                contract.calibration_cases,
                contract.output_class,
                pending.tier,
                purpose=contract.purpose,
            )
        )
        self.registry.complete_claimed_calibration(
            InsightClaimedPromptCalibrationRequest(
                pending.slot_id,
                pending.version,
                lease_ref,
                int(self.wall()),
                result,
            )
        )
        report_operation(
            OperationLog(
                operation=_OPERATION,
                component=_COMPONENT,
                phase="terminal",
                outcome=result.outcome.value,
                severity=LogSeverity.INFO,
                details={
                    "slot_id": pending.slot_id.value,
                    "version": pending.version,
                    "tier": pending.tier.value,
                    "case_count": len(result.cases),
                    "receive_count": receive_count,
                },
            ),
            LOGGER,
        )
        return True


@dataclass(frozen=True, slots=True)
class InsightPromptCalibrationOutboxDispatcher:
    """Deliver retained outbox identities to SQS, converging send-before-ack duplicates."""

    registry: InsightPromptRegistryPort
    queue: InsightPromptCalibrationQueuePort
    slot_ids: tuple[InsightSlotId, ...]

    def dispatch(self) -> int:
        """Send every retained identity and acknowledge each only after queue acceptance."""
        dispatched = 0
        for work in self.registry.pending_calibration_outbox(self.slot_ids):
            self.queue.enqueue(work.slot_id, work.version)
            self.registry.acknowledge_calibration_outbox(work.slot_id, work.version)
            dispatched += 1
        return dispatched


def _report_failure(error: BaseException, *, receive_count: int) -> None:
    report_error(
        error,
        Report(operation=_OPERATION, correlation_id="", logger=LOGGER),
    )
    report_operation(
        OperationLog(
            operation=_OPERATION,
            component=_COMPONENT,
            phase="terminal",
            outcome="retry",
            severity=LogSeverity.WARNING,
            details={"receive_count": receive_count},
        ),
        LOGGER,
    )


def _receive_count(record: dict[str, object]) -> int:
    attributes = record.get("attributes")
    if not isinstance(attributes, dict):
        return 0
    raw = cast("dict[str, object]", attributes).get("ApproximateReceiveCount")
    if not isinstance(raw, str):
        return 0
    try:
        value = int(raw)
    except ValueError:
        return 0
    return value if value > 0 else 0


def apply_records(event: object, application: CalibrationApplication) -> tuple[str, ...]:
    """Return only records that need SQS retry; terminal duplicates acknowledge cheaply."""
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
        message_id = record.get("messageId")
        if not isinstance(message_id, str) or not message_id:
            continue
        receive_count = _receive_count(record)
        try:
            acknowledge = application.apply(
                record.get("body"),
                lease_ref=f"{message_id}:{receive_count}:{uuid4().hex}",
                receive_count=receive_count,
            )
        except Exception as error:
            _report_failure(error, receive_count=receive_count)
            acknowledge = False
        if not acknowledge:
            failures.append(message_id)
    return tuple(failures)


@lru_cache(maxsize=1)
def application() -> InsightPromptCalibrationApplication:
    """Compose the queue worker's registry and patient Bedrock calibration authority."""
    settings = insight_prompt_calibration_worker_settings()
    return InsightPromptCalibrationApplication(
        registry=DynamoInsightPromptRegistry(settings.insight_prompt_registry_table),
        runner=BedrockInsightCalibrationRunner(
            InsightModelRouteModels(
                settings.insight_quick_model_id, settings.insight_deep_model_id
            ),
            settings.insight_generation_timeout_seconds,
        ),
        contracts={
            contract.id: contract for _, contract in load_insight_contracts(contracts_root())
        },
        lease_seconds=settings.insight_prompt_calibration_lease_seconds,
    )


@lru_cache(maxsize=1)
def dispatcher() -> InsightPromptCalibrationOutboxDispatcher:
    """Compose the scheduled retained-outbox dispatcher."""
    settings = insight_prompt_calibration_worker_settings()
    return InsightPromptCalibrationOutboxDispatcher(
        registry=DynamoInsightPromptRegistry(settings.insight_prompt_registry_table),
        queue=SqsInsightPromptCalibrationQueue(settings.insight_prompt_calibration_queue_url),
        slot_ids=tuple(contract.id for _, contract in load_insight_contracts(contracts_root())),
    )


@trace_boundary(INSIGHT_PROMPT_CALIBRATION)
@flushes_traces
@flushes_metrics
@LOGGER.inject_lambda_context
def handler(event: dict[str, Any], _context: object) -> dict[str, list[dict[str, str]]]:
    """Return failed queue identities so SQS performs bounded redrive and DLQ delivery."""
    with span(INSIGHT_PROMPT_CALIBRATION, operation=_OPERATION) as process_span:
        if "Records" not in event:
            try:
                dispatched_count = dispatcher().dispatch()
            except Exception as error:
                process_span.note_failure(error)
                _report_failure(error, receive_count=0)
                raise
            process_span.record(outcome="dispatched", dispatched_count=dispatched_count)
            report_operation(
                OperationLog(
                    operation=_OPERATION,
                    component=_COMPONENT,
                    phase="terminal",
                    outcome="dispatched",
                    severity=LogSeverity.INFO,
                    details={"dispatched_count": dispatched_count},
                ),
                LOGGER,
            )
            return {"batchItemFailures": []}
        raw_records: object = event.get("Records", [])
        record_count = (
            len(cast("list[object]", raw_records)) if isinstance(raw_records, list) else 0
        )
        failures = apply_records(event, application())
        failed_count = len(failures)
        if failures:
            process_span.note_failure(RuntimeError("partial batch retry"))
        outcome = "completed" if not failures else "partial_retry"
        process_span.record(
            outcome=outcome,
            record_count=record_count,
            acknowledged_count=record_count - failed_count,
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
                    "acknowledged_count": record_count - failed_count,
                    "failed_count": failed_count,
                },
            ),
            LOGGER,
        )
        return {"batchItemFailures": [{"itemIdentifier": item} for item in failures]}
