# SPDX-License-Identifier: MIT-0
"""Lease durable evaluation outbox rows and apply the dispatch rule to each (ADR-079).

The dispatcher is the only switch that decides what reaches evaluation. Every run reads each
family's release record once, before it claims anything, and a record it cannot read fails the
run. Each claimed row is then sent, withheld as not dispatched, or released to wait for its
delivery companion, exactly as `domain.evaluation.dispatch` decides.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from aws_lambda_powertools import Logger

from adapters.config import evaluation_outbox_dispatcher_settings, tracing_settings
from adapters.outbound.dynamodb.audit import DynamoAgentQualityAuditStore
from adapters.outbound.dynamodb.evaluation_outbox import DynamoEvaluationOutboxDispatchStore
from adapters.outbound.sqs.evaluation import SqsEvaluationSink
from adapters.outbound.ssm.evaluation_release import SsmEvaluationReleaseReader
from adapters.outbound.telemetry.bootstrap import configure_tracing, flushes_traces
from adapters.outbound.telemetry.reporting import (
    EvaluationDispatchOutcome,
    LogSeverity,
    OperationLog,
    Report,
    flushes_metrics,
    report_error,
    report_evaluation_dispatch,
    report_operation,
)
from adapters.outbound.telemetry.tracing import (
    EVALUATION_OUTBOX_DISPATCH,
    span,
    trace_boundary,
)
from domain.conversation.audit import RecordRetention
from domain.evaluation.dispatch import (
    EvaluationDispatchActionV1,
    EvaluationDispatchRequest,
    decide_dispatch,
    enabled_delivery_families,
)
from domain.evaluation.linkage import EvaluationDeliveryFamilyV1
from ports.outbound.audit import TurnDeliveryReaderPort
from ports.outbound.evaluation_outbox import (
    EvaluationOutboxClaimRequest,
    EvaluationOutboxDeliverRequest,
    EvaluationOutboxDueRequest,
    EvaluationOutboxReleaseRequest,
    EvaluationOutboxWithholdRequest,
    EvaluationOutboxWorkItem,
)

_SETTINGS = evaluation_outbox_dispatcher_settings()
_TRACING_SETTINGS = tracing_settings()
_TRACING = configure_tracing(_TRACING_SETTINGS)
_LOGGER = Logger(service="adlytics-evaluation-outbox-dispatcher")
_OPERATION = EVALUATION_OUTBOX_DISPATCH
_COMPONENT = "evaluation_outbox_dispatcher"
_STORE = DynamoEvaluationOutboxDispatchStore(
    _SETTINGS.outbox_table,
    _SETTINGS.deliveries_table,
    retention=RecordRetention(_SETTINGS.receipt_retention_days),
)
_SINK = SqsEvaluationSink(_SETTINGS.queue_url)
_RELEASES = SsmEvaluationReleaseReader(_SETTINGS.release_parameters)
_TURN_DELIVERIES: TurnDeliveryReaderPort = DynamoAgentQualityAuditStore(_SETTINGS.audit_table)
_LEASE_SECONDS = 120
_MAX_BACKOFF_SECONDS = 300
_DELIVERY_POLL_SECONDS = 30
"""How soon a row waiting for its delivery companion becomes due again."""

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
    _LOGGER,
)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _report_failure(error: BaseException) -> None:
    report_error(error, Report(operation=_OPERATION, correlation_id="", logger=_LOGGER))


def _terminal_expiry(decided: datetime) -> int:
    """Compute the delivered-row expiry, which a not-dispatched row receives too (ADR-079)."""
    return int((decided + timedelta(days=_SETTINGS.delivered_retention_days)).timestamp())


@dataclass(slots=True)
class _Counts:
    delivered: int = 0
    not_dispatched: int = 0
    waiting: int = 0
    retried: int = 0
    skipped: int = 0

    def details(self, due: int) -> dict[str, int]:
        return {
            "due_count": due,
            "delivered_count": self.delivered,
            "not_dispatched_count": self.not_dispatched,
            "waiting_count": self.waiting,
            "retry_count": self.retried,
            "skipped_count": self.skipped,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class _Claimed:
    work: EvaluationOutboxWorkItem
    lease_ref: str
    families: tuple[EvaluationDeliveryFamilyV1, ...]


def _send(
    claimed: _Claimed, counts: _Counts, note_failure: Callable[[BaseException], None]
) -> None:
    """Enqueue, then settle as delivered; a failed enqueue releases the lease with backoff.

    Known limit (ADR-079): the enqueue and the settlement are separate writes. A run that
    enqueues and then dies, or fails to settle, leaves the row leased; it is re-claimed after
    the lease and re-decided against that run's release records, so if the family was disabled
    in between, the row is withheld and records `not_dispatched` although its envelope already
    reached the writer. Any result it produces stays unlinked: the writer keeps its own receipt,
    and the ingress admits a result only for a configuration and evaluator the turn's delivery
    receipt names as dispatched, which a `not_dispatched` receipt never does.
    """
    work = claimed.work
    try:
        # The route admits each row with its receipt's creation time as `admitted_at`; the
        # writer computes its record's expiry from it.
        _SINK.enqueue(work.envelope.envelope, work.envelope.admitted_at)
    except Exception as error:
        note_failure(error)
        _report_failure(error)
        delay = min(_MAX_BACKOFF_SECONDS, 2 ** min(work.state.attempt_count, 8))
        _STORE.release(
            EvaluationOutboxReleaseRequest(
                admission_ref=work.envelope.admission_ref,
                lease_ref=claimed.lease_ref,
                available_at=_iso(datetime.now(UTC) + timedelta(seconds=delay)),
            )
        )
        counts.retried += 1
        report_evaluation_dispatch(EvaluationDispatchOutcome.RETRY)
        return
    decided = datetime.now(UTC)
    _STORE.deliver(
        EvaluationOutboxDeliverRequest(
            admission_ref=work.envelope.admission_ref,
            lease_ref=claimed.lease_ref,
            decided_at=_iso(decided),
            expires_at_epoch=_terminal_expiry(decided),
            families=claimed.families,
        )
    )
    counts.delivered += 1
    report_evaluation_dispatch(EvaluationDispatchOutcome.DISPATCHED)


def _dispatch(
    claimed: _Claimed, counts: _Counts, note_failure: Callable[[BaseException], None]
) -> None:
    """Apply the dispatch rule to one claimed row and record its outcome."""
    work = claimed.work
    # The delivery companion is read only when some family is enabled: a row that nothing
    # would evaluate is withheld without touching the receipts table.
    disposition = (
        _TURN_DELIVERIES.get_delivery(work.envelope.audit_id) if claimed.families else None
    )
    now = datetime.now(UTC)
    decision = decide_dispatch(
        EvaluationDispatchRequest(
            families=claimed.families,
            disposition=None if disposition is None else disposition.disposition,
            admitted_at=datetime.fromisoformat(work.envelope.admitted_at),
            now=now,
            delivery_wait_seconds=_SETTINGS.delivery_wait_seconds,
        )
    )
    if decision.action is EvaluationDispatchActionV1.SEND:
        _send(claimed, counts, note_failure)
    elif decision.action is EvaluationDispatchActionV1.WAIT:
        _STORE.release(
            EvaluationOutboxReleaseRequest(
                admission_ref=work.envelope.admission_ref,
                lease_ref=claimed.lease_ref,
                available_at=_iso(now + timedelta(seconds=_DELIVERY_POLL_SECONDS)),
                count_attempt=False,
            )
        )
        counts.waiting += 1
        report_evaluation_dispatch(EvaluationDispatchOutcome.WAITING)
    elif decision.reason is not None:
        _STORE.withhold(
            EvaluationOutboxWithholdRequest(
                admission_ref=work.envelope.admission_ref,
                lease_ref=claimed.lease_ref,
                decided_at=_iso(now),
                reason=decision.reason,
                expires_at_epoch=_terminal_expiry(now),
            )
        )
        counts.not_dispatched += 1
        report_evaluation_dispatch(EvaluationDispatchOutcome(decision.reason.value))


@trace_boundary(EVALUATION_OUTBOX_DISPATCH)
@flushes_traces
@flushes_metrics
@_LOGGER.inject_lambda_context
def handler(_event: dict[str, Any], _context: Any) -> dict[str, int]:
    """Dispatch bounded due work without logging evaluation content."""
    with span(EVALUATION_OUTBOX_DISPATCH, operation=_OPERATION) as process_span:
        counts = _Counts()
        references: tuple[str, ...] = ()
        try:
            # Read before any claim: an unreadable release record fails the run, so no row is
            # sent or withheld on a decision the dispatcher could not see.
            families = enabled_delivery_families(_RELEASES.read())
            started = datetime.now(UTC)
            references = _STORE.due(
                EvaluationOutboxDueRequest(ready_at_or_before=_iso(started), limit=10)
            )
            for admission_ref in references:
                acquired = datetime.now(UTC)
                lease_ref = uuid.uuid4().hex
                work = _STORE.claim(
                    EvaluationOutboxClaimRequest(
                        admission_ref=admission_ref,
                        lease_ref=lease_ref,
                        acquired_at=_iso(acquired),
                        expires_at=_iso(acquired + timedelta(seconds=_LEASE_SECONDS)),
                    )
                )
                if work is None:
                    counts.skipped += 1
                    continue
                _dispatch(
                    _Claimed(work=work, lease_ref=lease_ref, families=families),
                    counts,
                    process_span.note_failure,
                )
        except Exception as error:
            process_span.record(
                outcome="failed",
                due_count=len(references),
                delivered_count=counts.delivered,
                not_dispatched_count=counts.not_dispatched,
                waiting_count=counts.waiting,
                retry_count=counts.retried,
                skipped_count=counts.skipped,
            )
            _report_failure(error)
            raise
        outcome = "retry" if counts.retried else "completed"
        process_span.record(
            outcome=outcome,
            due_count=len(references),
            delivered_count=counts.delivered,
            not_dispatched_count=counts.not_dispatched,
            waiting_count=counts.waiting,
            retry_count=counts.retried,
            skipped_count=counts.skipped,
        )
        report_operation(
            OperationLog(
                operation=_OPERATION,
                component=_COMPONENT,
                phase="terminal",
                outcome=outcome,
                severity=LogSeverity.WARNING if counts.retried else LogSeverity.INFO,
                details=counts.details(len(references)),
            ),
            _LOGGER,
        )
        return {
            "due": len(references),
            "delivered": counts.delivered,
            "not_dispatched": counts.not_dispatched,
            "waiting": counts.waiting,
            "retried": counts.retried,
        }
