# SPDX-License-Identifier: MIT-0
"""Protected CloudWatch subscription ingress for allowlisted evaluation results."""

from __future__ import annotations

from typing import Any

import boto3
from aws_lambda_powertools import Logger

from adapters.config import evaluation_result_ingress_settings, tracing_settings
from adapters.jobs.evaluation_result_ingress.subscription import (
    EvaluationResultCandidate,
    EvaluationResultStampRequest,
    candidates_from_subscription,
)
from adapters.jobs.evaluation_result_link.work import result_to_json
from adapters.outbound.dynamodb.audit import (
    DynamoAgentQualityAuditStore,
    DynamoEvaluationDeliveryStore,
    DynamoEvaluationResultLinkResolver,
    DynamoEvaluationWriterReceiptStore,
)
from adapters.outbound.sqs.attributes import receipt_created_at_message_attribute
from adapters.outbound.telemetry.bootstrap import (
    configure_tracing,
    flushes_traces,
    otlp_traces_endpoint,
)
from adapters.outbound.telemetry.reporting import (
    METRICS,
    EvaluationIngressLog,
    LifecycleOutcome,
    LogSeverity,
    OperationLog,
    Report,
    report_error,
    report_lifecycle,
    report_operation,
)
from adapters.outbound.telemetry.tracing import EVALUATION_RESULT_INGRESS, span, trace_boundary
from domain.evaluation.linkage import EvaluationDeliveryReceiptV2
from ports.outbound.audit import (
    EvaluationDeliveryStorePort,
    EvaluationResultLinkRequest,
    EvaluationResultLinkResolutionState,
    EvaluationResultLinkTarget,
)

_SETTINGS = evaluation_result_ingress_settings()
_TRACING_SETTINGS = tracing_settings()
_TRACING = configure_tracing(_TRACING_SETTINGS)
_LOGGER = Logger(service="adlytics-evaluation-result-ingress")
_OPERATION = "evaluation_result.ingress"
_RESOLVER = DynamoEvaluationResultLinkResolver(
    DynamoAgentQualityAuditStore(_SETTINGS.audit_table),
    DynamoEvaluationWriterReceiptStore(_SETTINGS.writer_receipts_table),
)
_DELIVERIES: EvaluationDeliveryStorePort = DynamoEvaluationDeliveryStore(_SETTINGS.deliveries_table)
_SQS = boto3.client("sqs")

report_operation(
    OperationLog(
        operation=_OPERATION,
        component="evaluation_ingress",
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


class _PendingLinkError(Exception):
    """Fixed retry signal; it never carries protected event material."""


_WRITER_PENDING = "evaluation writer receipt is pending"
_DELIVERY_PENDING = "evaluation delivery receipt is pending"


class _Absent:
    """The turn's delivery receipt has not been written yet: retry, never ignore."""


_ABSENT = _Absent()


def _stamp(
    candidate: EvaluationResultCandidate, target: EvaluationResultLinkTarget
) -> EvaluationResultStampRequest | _Absent | None:
    """Admit a result only for a pair the turn was dispatched to (ADR-079); `None` ignores it.

    The dispatcher sends a row before it writes the receipt, so a result can in principle
    arrive first; an absent receipt is therefore a retry. A receipt that predates the gate, a
    not-dispatched receipt, or one that never named this configuration and evaluator is final.
    """
    receipt = _DELIVERIES.get_evaluation_delivery(target.audit_id, target.admission_ref)
    if receipt is None:
        return _ABSENT
    if not isinstance(receipt, EvaluationDeliveryReceiptV2):
        return None
    family = receipt.dispatched_family(candidate.config_id)
    evaluator = None if family is None else family.evaluator(candidate.evaluator_ref)
    if family is None or evaluator is None:
        return None
    return EvaluationResultStampRequest(
        audit_id=target.audit_id,
        admission_ref=target.admission_ref,
        family=family,
        evaluator=evaluator,
    )


def _report_ignored() -> None:
    report_lifecycle(
        EvaluationIngressLog(outcome=LifecycleOutcome.IGNORED, ignored_count=1),
        _LOGGER,
    )


def _report_failure(error: Exception) -> None:
    report_error(error, Report(operation=_OPERATION, correlation_id="", logger=_LOGGER))


def _ingest(event: object) -> tuple[int, int]:
    """Project eligible events; only a writer- or receipt-order race requests source retry.

    Every candidate in the batch is decided first and every admissible one is enqueued; only
    then does a pending one request the retry, so one early result never costs the batch's
    others. The retry re-enqueues the ones already sent, which the linker's exact replay absorbs.
    """
    candidates = candidates_from_subscription(
        event, expected_log_groups=_SETTINGS.result_log_groups
    )
    linked = 0
    ignored = 0
    pending: list[str] = []
    for candidate in candidates:
        resolution = _RESOLVER.resolve_evaluation_result(
            EvaluationResultLinkRequest(
                correlation_id=candidate.correlation_id,
                trace_id=candidate.trace_id,
            )
        )
        if resolution.state is EvaluationResultLinkResolutionState.PENDING:
            pending.append(_WRITER_PENDING)
            continue
        if (
            resolution.state is EvaluationResultLinkResolutionState.UNMATCHED
            or resolution.target is None
        ):
            ignored += 1
            _report_ignored()
            continue
        stamp = _stamp(candidate, resolution.target)
        if isinstance(stamp, _Absent):
            pending.append(_DELIVERY_PENDING)
            continue
        if stamp is None:
            ignored += 1
            _report_ignored()
            continue
        result = candidate.result(stamp)
        # The canonical result bytes stay the body; the receipt's creation time rides beside
        # them so the linker, which never reads receipts, can compute the verdict's expiry.
        _SQS.send_message(
            QueueUrl=_SETTINGS.projection_queue_url,
            MessageBody=result_to_json(result),
            MessageAttributes=receipt_created_at_message_attribute(
                resolution.target.receipt_created_at
            ),
        )
        linked += 1
        report_lifecycle(
            EvaluationIngressLog(
                outcome=LifecycleOutcome.LINKED,
                audit_id=resolution.target.audit_id,
                correlation_id=candidate.correlation_id,
                admission_ref=resolution.target.admission_ref,
                evaluator_ref=candidate.evaluator_ref,
                evaluator_name=result.evaluator_name,
                result_state=candidate.state.value,
                quality_label="" if candidate.label is None else candidate.label,
                quality_score=candidate.score,
                linked_count=1,
            ),
            _LOGGER,
        )
    if pending:
        raise _PendingLinkError(pending[0])
    return linked, ignored


@trace_boundary(EVALUATION_RESULT_INGRESS)
@flushes_traces
@METRICS.log_metrics
@_LOGGER.inject_lambda_context
def handler(event: dict[str, Any], _context: Any) -> None:
    """Receive raw subscription bytes transiently and emit only safe result-contract bytes."""
    with span(EVALUATION_RESULT_INGRESS, operation=_OPERATION) as process_span:
        try:
            linked, ignored = _ingest(event)
        except _PendingLinkError as error:
            _report_failure(error)
            report_lifecycle(EvaluationIngressLog(outcome=LifecycleOutcome.RETRY), _LOGGER)
            raise
        except ValueError as error:
            # Malformed and unrelated source events are acknowledged; no raw envelope reaches a DLQ.
            process_span.note_failure(error)
            _report_failure(error)
            report_lifecycle(
                EvaluationIngressLog(outcome=LifecycleOutcome.IGNORED, ignored_count=1),
                _LOGGER,
            )
            process_span.record(outcome="ignored", linked_count=0, ignored_count=1)
            return
        except Exception as error:
            _report_failure(error)
            report_lifecycle(EvaluationIngressLog(outcome=LifecycleOutcome.RETRY), _LOGGER)
            raise
        process_span.record(
            outcome="completed",
            linked_count=linked,
            ignored_count=ignored,
        )
