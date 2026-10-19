# SPDX-License-Identifier: MIT-0
"""Bounded scheduled reconciliation through the same operational-status reducer."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from aws_lambda_powertools import Logger

from adapters.config import operational_reconciler_settings, tracing_settings
from adapters.jobs.operational_status.attempts import DynamoAttemptAuthority
from adapters.outbound.dynamodb.operational_status import DynamoOperationalStatusStore
from adapters.outbound.sqs.operational_status import SqsOperationalStatusSink
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
    OPERATIONAL_STATUS_RECONCILER,
    span,
    trace_boundary,
)
from domain.operations.generated_data_flows import SURFACES
from domain.operations.status import (
    MEASUREMENT_REFS,
    BaselineFoldRequest,
    ReductionKind,
    corrected_baseline,
    initial_baseline,
    window_sample_count,
)
from ports.outbound.operational_status import OperationalProjectionPort, OperationalStatusSink

_PENDING_LIMIT = 10
_SETTINGS = operational_reconciler_settings()
_TRACING_SETTINGS = tracing_settings()
_TRACING = configure_tracing(_TRACING_SETTINGS)
_LOGGER = Logger(service="adlytics-operational-status-reconciler")
_OPERATION = OPERATIONAL_STATUS_RECONCILER
_COMPONENT = "operational_status_reconciler"

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


@dataclass(frozen=True, slots=True)
class ReconciliationCounts:
    """Bounded reconciliation disposition counts."""

    queried: int
    applied: int
    parked: int
    failed: int
    windows_written: int = 0


def _surface_status(surface_id: str) -> str:
    return next(
        cast("str", surface["status"]) for surface in SURFACES if surface["id"] == surface_id
    )


@dataclass(frozen=True, slots=True)
class Reconciler:
    """Query only live pending work and invoke the projector reducer directly."""

    store: OperationalProjectionPort
    sink: OperationalStatusSink
    attempt_authority: DynamoAttemptAuthority

    def run(self, now: datetime | None = None) -> ReconciliationCounts:
        """Reduce one bounded pending page, re-admit parked events, and close windows."""
        page = self.store.pending(_PENDING_LIMIT)
        applied = parked = failed = 0
        for event in page.events:
            try:
                outcome = self.store.reduce(event)
                if outcome.kind in {ReductionKind.APPLY, ReductionKind.ALREADY_APPLIED}:
                    applied += 1
                else:
                    parked += 1
                    # The attempts producer re-sends complete deterministic history on every
                    # authority transition. Re-admit this exact immutable event so a concurrently
                    # restored predecessor causes another bounded reducer pass without a table scan.
                    self.sink.send(event)
            except Exception as error:
                failed += 1
                report_error(error, Report(operation=_OPERATION, correlation_id="", logger=_LOGGER))
        windows = self.close_windows(now or datetime.now(tz=UTC))
        return ReconciliationCounts(len(page.events), applied, parked, failed, windows)

    def close_windows(self, now: datetime) -> int:
        """Fold the last closed hour into one immutable descriptive window per measurement.

        Idempotent per run: an existing revision with the same recount is left alone, a
        first fold is a conditional create, and a recount that grew (late contributions)
        appends a correction revision. Failures are reported and never block reduction.
        """
        window_end = now.replace(minute=0, second=0, microsecond=0)
        window_start = window_end - timedelta(hours=1)
        written = 0
        for surface_id, references in sorted(MEASUREMENT_REFS.items()):
            if not references or _surface_status(surface_id) != "active":
                continue
            try:
                contributions = self.store.window_contributions(
                    surface_id, window_start, window_end
                )
                count = window_sample_count(contributions, window_start, window_end)
                for reference, version in references:
                    written += self._fold(
                        surface_id,
                        BaselineFoldRequest(
                            measurement_ref=reference,
                            measurement_version=version,
                            window_start=window_start,
                            window_end=window_end,
                            finalized_at=now,
                            sample_count=count,
                        ),
                    )
            except Exception as error:
                report_error(error, Report(operation=_OPERATION, correlation_id="", logger=_LOGGER))
        return written

    def _fold(self, surface_id: str, request: BaselineFoldRequest) -> int:
        existing = [
            item
            for item in self.store.baselines(surface_id, request.measurement_ref)
            if item.window_start == request.window_start
        ]
        if not existing:
            return int(self.store.record_baseline(surface_id, initial_baseline(request)))
        latest = max(existing, key=lambda item: item.revision)
        if request.sample_count <= latest.sample_count:
            return 0
        corrected = corrected_baseline(
            latest, finalized_at=request.finalized_at, sample_count=request.sample_count
        )
        return int(self.store.record_baseline(surface_id, corrected))


_RECONCILER = Reconciler(
    store=DynamoOperationalStatusStore(_SETTINGS.status_table),
    sink=SqsOperationalStatusSink(_SETTINGS.queue_url),
    attempt_authority=DynamoAttemptAuthority(_SETTINGS.attempts_table),
)


@trace_boundary(OPERATIONAL_STATUS_RECONCILER)
@flushes_traces
@flushes_metrics
@_LOGGER.inject_lambda_context
def handler(_event: dict[str, Any], _context: object) -> dict[str, int]:
    """Resume bounded pending work without Scan or an alternate reduction path."""
    with span(OPERATIONAL_STATUS_RECONCILER, operation=_OPERATION) as process_span:
        counts = _RECONCILER.run()
        outcome = "completed" if counts.failed == 0 else "partial_retry"
        process_span.record(
            outcome=outcome,
            queried_count=counts.queried,
            applied_count=counts.applied,
            parked_count=counts.parked,
            failed_count=counts.failed,
            windows_written=counts.windows_written,
        )
        report_operation(
            OperationLog(
                operation=_OPERATION,
                component=_COMPONENT,
                phase="terminal",
                outcome=outcome,
                severity=LogSeverity.INFO if counts.failed == 0 else LogSeverity.WARNING,
                details={
                    "queried_count": counts.queried,
                    "applied_count": counts.applied,
                    "parked_count": counts.parked,
                    "failed_count": counts.failed,
                    "windows_written": counts.windows_written,
                },
            ),
            _LOGGER,
        )
        return {
            "queried": counts.queried,
            "applied": counts.applied,
            "parked": counts.parked,
            "failed": counts.failed,
            "windows_written": counts.windows_written,
        }
