# SPDX-License-Identifier: MIT-0
"""Persist only canonical content-free evaluation-result projections from SQS."""

from __future__ import annotations

from typing import Any, cast

from aws_lambda_powertools import Logger

from adapters.config import evaluation_result_persister_settings, tracing_settings
from adapters.jobs.evaluation_result_link.work import result_from_json
from adapters.outbound.dynamodb.audit import DynamoEvaluationVerdictStore
from adapters.outbound.sqs.attributes import receipt_created_at_attribute
from adapters.outbound.telemetry.bootstrap import (
    configure_tracing,
    flushes_traces,
    otlp_traces_endpoint,
)
from adapters.outbound.telemetry.reporting import (
    EvaluationPersisterLog,
    LifecycleOutcome,
    LogSeverity,
    OperationLog,
    Report,
    flushes_metrics,
    report_error,
    report_evaluation_verdict,
    report_lifecycle,
    report_operation,
)
from adapters.outbound.telemetry.tracing import EVALUATION_RESULT_PERSIST, span, trace_boundary
from domain.conversation.audit import RecordRetention
from domain.evaluation.quality import verdict_count
from domain.shared_kernel.errors import AdlyticsError
from ports.outbound.audit import AuditWriteDisposition

_SETTINGS = evaluation_result_persister_settings()
_TRACING_SETTINGS = tracing_settings()
_TRACING = configure_tracing(_TRACING_SETTINGS)
_LOGGER = Logger(service="adlytics-evaluation-result-persister")
_OPERATION = "evaluation_result.persist"
_VERDICTS = DynamoEvaluationVerdictStore(
    _SETTINGS.verdicts_table, retention=RecordRetention(_SETTINGS.receipt_retention_days)
)

report_operation(
    OperationLog(
        operation=_OPERATION,
        component="evaluation_persister",
        phase="start",
        outcome="started",
        severity=LogSeverity.INFO,
        details={
            "tracing": _TRACING,
            "traces_endpoint": (
                otlp_traces_endpoint(_TRACING_SETTINGS.aws_region) if _TRACING else None
            ),
            "sample_ratio": _TRACING_SETTINGS.sample_ratio,
        },
    ),
    _LOGGER,
)


def _report_failure(error: Exception) -> None:
    report_error(error, Report(operation=_OPERATION, correlation_id="", logger=_LOGGER))


@trace_boundary(EVALUATION_RESULT_PERSIST)
@flushes_traces
@flushes_metrics
@_LOGGER.inject_lambda_context
def handler(event: dict[str, Any], _context: Any) -> dict[str, list[dict[str, str]]]:
    """Persist only strict EvaluationResult.v3 queue messages; retry safe records individually."""
    with span(EVALUATION_RESULT_PERSIST, operation=_OPERATION) as process_span:
        records = event.get("Records", [])
        if not isinstance(records, list):
            error = ValueError("evaluation result projection records are malformed")
            _report_failure(error)
            process_span.record(outcome="retry", record_count=0, failure_count=1)
            return {"batchItemFailures": [{"itemIdentifier": "invalid-event"}]}
        projection_records = cast("list[object]", records)
        failures: list[dict[str, str]] = []
        for raw in projection_records:
            identifier = "unknown"
            audit_id = ""
            try:
                if not isinstance(raw, dict):
                    raise ValueError("evaluation result projection record is malformed")
                record = cast("dict[str, object]", raw)
                identifier = str(record.get("messageId", "unknown"))
                result = result_from_json(record.get("body"))
                receipt_created_at = receipt_created_at_attribute(record)
                audit_id = result.audit_id
                written = _VERDICTS.write_evaluation_result(result, receipt_created_at)
                # ADR-079 §5: the quality counts, once per newly linked completed verdict. A
                # redelivered message replays exactly and is not counted again.
                if written.disposition is AuditWriteDisposition.APPLIED and result.label:
                    report_evaluation_verdict(verdict_count(result.evaluator_name, result.label))
                report_lifecycle(
                    EvaluationPersisterLog(
                        audit_id=audit_id,
                        outcome=LifecycleOutcome.PERSISTED,
                        persisted_count=1,
                    ),
                    _LOGGER,
                )
            except Exception as error:
                if isinstance(error, AdlyticsError):
                    process_span.note_error(error)
                else:
                    process_span.note_failure(error)
                _report_failure(error)
                report_lifecycle(
                    EvaluationPersisterLog(
                        audit_id=audit_id,
                        outcome=LifecycleOutcome.RETRY,
                        persisted_count=0,
                    ),
                    _LOGGER,
                )
                failures.append({"itemIdentifier": identifier})
        process_span.record(
            outcome="completed" if not failures else "retry",
            record_count=len(projection_records),
            failure_count=len(failures),
        )
        return {"batchItemFailures": failures}
