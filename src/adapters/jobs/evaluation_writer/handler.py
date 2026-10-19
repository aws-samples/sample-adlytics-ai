# SPDX-License-Identifier: MIT-0
"""Write exact released-response envelopes as AgentCore-compatible generic OTel spans."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import boto3
from aws_lambda_powertools import Logger
from botocore.exceptions import ClientError

from adapters.config import evaluation_writer_settings, tracing_settings
from adapters.jobs.evaluation_writer.contract import decode_envelope, released_span_identifiers
from adapters.jobs.evaluation_writer.span import ReleasedSpanProjection, released_span_json
from adapters.outbound.dynamodb.audit import AUDIT_CONFLICT, DynamoEvaluationWriterReceiptStore
from adapters.outbound.sqs.attributes import receipt_created_at_attribute
from adapters.outbound.telemetry.bootstrap import configure_tracing, flushes_traces
from adapters.outbound.telemetry.reporting import (
    LogSeverity,
    OperationLog,
    Report,
    flushes_metrics,
    report_error,
    report_operation,
)
from adapters.outbound.telemetry.tracing import EVALUATION_WRITER, span, trace_boundary
from domain.conversation.audit import RecordRetention
from domain.evaluation.linkage import EvaluationWriterReceiptV1
from domain.shared_kernel.errors import InvariantError

if TYPE_CHECKING:
    from types_boto3_logs import CloudWatchLogsClient

    from domain.evaluation.envelope import EvaluationEnvelope

_SETTINGS = evaluation_writer_settings()
_TRACING_SETTINGS = tracing_settings()
_TRACING = configure_tracing(_TRACING_SETTINGS)
LOG_GROUP = _SETTINGS.input_log_group
LOG_STREAM = "released-responses"
SERVICE_NAME = _SETTINGS.service_name
LOGGER = Logger(service="adlytics-evaluation-writer")
_OPERATION = EVALUATION_WRITER
_COMPONENT = "evaluation_writer"

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

_LOGS: CloudWatchLogsClient = boto3.client("logs")
_WRITER_RECEIPTS = DynamoEvaluationWriterReceiptStore(
    _SETTINGS.writer_receipts_table,
    retention=RecordRetention(_SETTINGS.receipt_retention_days),
)


def _ensure_stream() -> None:
    try:
        _LOGS.create_log_stream(logGroupName=LOG_GROUP, logStreamName=LOG_STREAM)
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "ResourceAlreadyExistsException":
            raise


def _write_released(envelope: EvaluationEnvelope, receipt_created_at: str) -> str:
    """Emit one released span and its writer receipt once; return the record outcome.

    ADR-080 §3: a redelivered message finds its own receipt first. `emitted_at` is the first
    attempt's clock, so the stored receipt is compared by what this message determines (the
    admission key, trace, and span) and the span is not emitted twice. A stored receipt naming
    another trace or span is a conflict: the admission was already evaluated as a different span.
    The conditional put still decides a concurrent first write.
    """
    trace_id, span_id = released_span_identifiers(envelope)
    stored = _WRITER_RECEIPTS.get_evaluation_writer_receipt(
        envelope.audit_id, envelope.evaluation_admission_ref
    )
    if stored is not None:
        if stored.trace_id != trace_id or stored.span_id != span_id:
            raise InvariantError(
                AUDIT_CONFLICT, "An evaluation writer reference names a different released span."
            )
        return "replayed"
    observed_at = datetime.now(UTC)
    timestamp_ns = int(observed_at.timestamp() * 1_000_000_000)
    released_span = released_span_json(
        ReleasedSpanProjection(
            envelope=envelope,
            service_name=SERVICE_NAME,
            timestamp_ns=timestamp_ns,
            trace_id=trace_id,
            span_id=span_id,
        )
    )
    _LOGS.put_log_events(
        logGroupName=LOG_GROUP,
        logStreamName=LOG_STREAM,
        logEvents=[{"timestamp": timestamp_ns // 1_000_000, "message": released_span}],
    )
    _WRITER_RECEIPTS.write_evaluation_writer_receipt(
        EvaluationWriterReceiptV1(
            audit_id=envelope.audit_id,
            admission_ref=envelope.evaluation_admission_ref,
            trace_id=trace_id,
            span_id=span_id,
            emitted_at=observed_at.isoformat(),
        ),
        receipt_created_at,
    )
    return "persisted"


def _report_failure(error: BaseException) -> None:
    report_error(error, Report(operation=_OPERATION, correlation_id="", logger=LOGGER))


@trace_boundary(EVALUATION_WRITER)
@flushes_traces
@flushes_metrics
@LOGGER.inject_lambda_context
def handler(event: dict[str, Any], _context: Any) -> dict[str, list[dict[str, str]]]:
    """Write valid SQS envelopes as generic AgentCore evaluation spans."""
    with span(EVALUATION_WRITER, operation=_OPERATION) as process_span:
        raw_records: object = event.get("Records", [])
        records = cast("list[object]", raw_records) if isinstance(raw_records, list) else None
        record_count = 0 if records is None else len(records)
        report_operation(
            OperationLog(
                operation=_OPERATION,
                component=_COMPONENT,
                phase="start",
                outcome="started",
                severity=LogSeverity.DEBUG,
                details={"record_count": record_count},
            ),
            LOGGER,
        )
        persisted = 0
        failures: list[dict[str, str]] = []
        try:
            _ensure_stream()
            if records is None:
                error = ValueError("evaluation writer event has no record list")
                process_span.note_failure(error)
                _report_failure(error)
                process_span.record(
                    outcome="refused",
                    record_count=record_count,
                    persisted_count=persisted,
                    failed_count=1,
                )
                report_operation(
                    OperationLog(
                        operation=_OPERATION,
                        component=_COMPONENT,
                        phase="terminal",
                        outcome="refused",
                        severity=LogSeverity.WARNING,
                        description="The evaluation writer event has no record list.",
                        details={"failed_count": 1},
                    ),
                    LOGGER,
                )
                return {"batchItemFailures": [{"itemIdentifier": "invalid-event"}]}
            for value in records:
                if not isinstance(value, dict):
                    error = ValueError("evaluation writer record is not an object")
                    process_span.note_failure(error)
                    _report_failure(error)
                    failures.append({"itemIdentifier": "invalid-record"})
                    report_operation(
                        OperationLog(
                            operation=_OPERATION,
                            component=_COMPONENT,
                            phase="record",
                            outcome="refused",
                            severity=LogSeverity.WARNING,
                            description="The evaluation writer record is not an object.",
                        ),
                        LOGGER,
                    )
                    continue
                record = cast("dict[str, object]", value)
                identifier = str(record.get("messageId", "unknown"))
                try:
                    envelope = decode_envelope(str(record["body"]))
                    receipt_created_at = receipt_created_at_attribute(record)
                    # A replay's receipt is already persisted; nothing is emitted or written.
                    record_outcome = _write_released(envelope, receipt_created_at)
                    persisted += 1
                    report_operation(
                        OperationLog(
                            operation=_OPERATION,
                            component=_COMPONENT,
                            phase="record",
                            outcome=record_outcome,
                            severity=LogSeverity.INFO,
                        ),
                        LOGGER,
                    )
                except Exception as error:
                    process_span.note_failure(error)
                    _report_failure(error)
                    report_operation(
                        OperationLog(
                            operation=_OPERATION,
                            component=_COMPONENT,
                            phase="record",
                            outcome="retry",
                            severity=LogSeverity.WARNING,
                        ),
                        LOGGER,
                    )
                    failures.append({"itemIdentifier": identifier})
        except Exception as error:
            process_span.record(
                outcome="failed",
                record_count=record_count,
                persisted_count=persisted,
                failed_count=len(failures),
            )
            _report_failure(error)
            raise
        outcome = "completed" if not failures else "partial_retry"
        process_span.record(
            outcome=outcome,
            record_count=record_count,
            persisted_count=persisted,
            failed_count=len(failures),
        )
        report_operation(
            OperationLog(
                operation=_OPERATION,
                component=_COMPONENT,
                phase="terminal",
                outcome=outcome,
                severity=LogSeverity.INFO if not failures else LogSeverity.WARNING,
                details={"record_count": record_count, "failed_count": len(failures)},
            ),
            LOGGER,
        )
        return {"batchItemFailures": failures}
