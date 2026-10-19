# SPDX-License-Identifier: MIT-0
"""Process KEYS_ONLY Agent Quality stream records into deterministic bounded findings."""

from __future__ import annotations

from typing import Any, cast

from aws_lambda_powertools import Logger

from adapters.config import agent_quality_processor_settings, tracing_settings
from adapters.outbound.dynamodb.audit import (
    AUDIT_ID,
    ITEM_KEY,
    RECORD,
    DynamoAgentQualityAggregateStore,
    DynamoAgentQualityAuditStore,
    DynamoAuditFindingStore,
)
from adapters.outbound.telemetry.bootstrap import (
    configure_tracing,
    flushes_traces,
    otlp_traces_endpoint,
)
from adapters.outbound.telemetry.reporting import (
    AuditProcessorLog,
    LifecycleOutcome,
    LogSeverity,
    OperationLog,
    Report,
    flushes_metrics,
    report_error,
    report_lifecycle,
    report_operation,
)
from adapters.outbound.telemetry.tracing import AGENT_QUALITY_PROCESS, span, trace_boundary
from domain.conversation.audit import RecordRetention
from domain.conversation.audit_findings import aggregate_metric, derive_findings
from domain.shared_kernel.errors import AdlyticsError

_SETTINGS = agent_quality_processor_settings()
_TRACING_SETTINGS = tracing_settings()
_TRACING = configure_tracing(_TRACING_SETTINGS)
_LOGGER = Logger(service="adlytics-agent-quality-processor")
_OPERATION = "agent_quality.processor"
_AUDIT_STORE = DynamoAgentQualityAuditStore(_SETTINGS.audit_table)
_FINDINGS_STORE = DynamoAuditFindingStore(
    _SETTINGS.findings_table, retention=RecordRetention(_SETTINGS.receipt_retention_days)
)
_AGGREGATE_STORE = DynamoAgentQualityAggregateStore(_SETTINGS.aggregate_table)

report_operation(
    OperationLog(
        operation=_OPERATION,
        component="audit_processor",
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


def _audit_id(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    record = cast("dict[str, object]", value)
    keys = record.get("dynamodb")
    if not isinstance(keys, dict):
        return None
    dynamodb = cast("dict[str, object]", keys)
    exact = dynamodb.get("Keys")
    if not isinstance(exact, dict):
        return None
    exact_keys = cast("dict[str, object]", exact)
    audit = exact_keys.get(AUDIT_ID)
    item_key = exact_keys.get(ITEM_KEY)
    if not isinstance(audit, dict) or not isinstance(item_key, dict):
        return None
    audit_value = cast("dict[str, object]", audit)
    item_key_value = cast("dict[str, object]", item_key)
    identifier = audit_value.get("S")
    key = item_key_value.get("S")
    return identifier if isinstance(identifier, str) and key == RECORD else None


def _report_failure(error: Exception, correlation_id: str) -> None:
    report_error(
        error,
        Report(operation=_OPERATION, correlation_id=correlation_id, logger=_LOGGER),
    )


@trace_boundary(AGENT_QUALITY_PROCESS)
@flushes_traces
@flushes_metrics
@_LOGGER.inject_lambda_context
def handler(event: dict[str, Any], _context: Any) -> dict[str, list[dict[str, str]]]:
    """Read only stream keys then exact receipts; return partial-batch failures safely."""
    with span(AGENT_QUALITY_PROCESS, operation=_OPERATION) as process_span:
        records = event.get("Records", [])
        if not isinstance(records, list):
            error = ValueError("agent quality stream records are malformed")
            _report_failure(error, "")
            process_span.record(
                outcome="retry",
                record_count=0,
                processed_count=0,
                failure_count=1,
            )
            return {"batchItemFailures": [{"itemIdentifier": "invalid-event"}]}
        stream_records = cast("list[object]", records)

        failures: list[dict[str, str]] = []
        processed = 0
        for raw in stream_records:
            identifier = "unknown"
            correlation_id = ""
            audit_id = ""
            try:
                if not isinstance(raw, dict):
                    raise ValueError("agent quality stream record is malformed")
                item = cast("dict[str, object]", raw)
                identifier = str(item.get("eventID", "unknown"))
                audit_id = _audit_id(item) or ""
                if not audit_id:
                    continue
                record = _AUDIT_STORE.get(audit_id)
                if record is None:
                    raise ValueError("agent quality stream receipt is absent")
                correlation_id = record.correlation_id
                findings = derive_findings(record)
                for finding in findings:
                    _FINDINGS_STORE.write_finding(finding, record.created_at)
                _AGGREGATE_STORE.write_metric(aggregate_metric(record))
                report_lifecycle(
                    AuditProcessorLog(
                        correlation_id=correlation_id,
                        audit_id=audit_id,
                        outcome=LifecycleOutcome.PROCESSED,
                        finding_count=len(findings),
                        aggregate_written=True,
                    ),
                    _LOGGER,
                )
                processed += 1
            except Exception as error:
                if isinstance(error, AdlyticsError):
                    process_span.note_error(error)
                else:
                    process_span.note_failure(error)
                _report_failure(error, correlation_id)
                report_lifecycle(
                    AuditProcessorLog(
                        correlation_id=correlation_id,
                        audit_id=audit_id,
                        outcome=LifecycleOutcome.RETRY,
                        finding_count=0,
                        aggregate_written=False,
                    ),
                    _LOGGER,
                )
                failures.append({"itemIdentifier": identifier})
        process_span.record(
            outcome="completed" if not failures else "retry",
            record_count=len(stream_records),
            processed_count=processed,
            failure_count=len(failures),
        )
        return {"batchItemFailures": failures}
