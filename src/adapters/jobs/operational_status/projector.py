# SPDX-License-Identifier: MIT-0
"""Dedicated FIFO projector for closed operational-status messages.

S2 adds `graph_handler`: the status v2 projection of execution-graph events. It consumes the
live graph table's stream (committed items only), maps each to at most one immutable row under
its root flow (`domain.operations.graph_projection`), and writes only the status table. It holds
no action on the graph table's items, so it never accepts or issues a graph command.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

import boto3
from aws_lambda_powertools import Logger

from adapters.config import operational_projector_settings, tracing_settings
from adapters.outbound.dynamodb.metric_store import DynamoMetricStore
from adapters.outbound.dynamodb.operational_status import (
    DynamoGraphProjectionStore,
    DynamoOperationalStatusStore,
    decode_item,
)
from adapters.outbound.dynamodb.stage1_operational_status import (
    DynamoStage1RetainedEventAuthorityReader,
)
from adapters.outbound.sqs.operational_status import parse_message
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
    OPERATIONAL_STATUS_PROJECTOR,
    span,
    trace_boundary,
)
from domain.operations.graph_projection import project_graph_item
from domain.operations.stage1_operational_status import (
    TERMINAL_FLOW_STATES,
    ChildOutcomeV2,
    Stage1Surface,
    verify_stage1_wire_member,
)
from domain.operations.status import (
    OperationalDataFlowEvent,
    OperationalStatusError,
    ReductionKind,
)
from ports.outbound.execution_explanation import GraphProjectionWriter
from ports.outbound.operational_status import (
    OperationalProjectionPort,
    PlanTerminalProjectionRequest,
)
from ports.outbound.stage1_operational_status import Stage1RetainedEventAuthorityPort

_SETTINGS = operational_projector_settings()
_TRACING_SETTINGS = tracing_settings()
_TRACING = configure_tracing(_TRACING_SETTINGS)
_LOGGER = Logger(service="adlytics-operational-status-projector")
_OPERATION = OPERATIONAL_STATUS_PROJECTOR
_COMPONENT = "operational_status_projector"

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
class Projector:
    """Parse one exact queue record and dispatch it through the conditional reducer paths."""

    store: OperationalProjectionPort
    retained: Stage1RetainedEventAuthorityPort

    def apply(self, body: object) -> None:
        """Reduce one member; Stage-1 unit/plan terminals join their retained authority."""
        message = parse_message(body)
        if isinstance(message, OperationalDataFlowEvent) and _stage1_v2_terminal(message):
            self._apply_stage1_terminal(message)
            return
        self.store.reduce(message)

    def _apply_stage1_terminal(self, event: OperationalDataFlowEvent) -> None:
        verified = self.retained.retained_event_authority(
            event.flow_id, event.authority.opaque_digest
        )
        if verified is None:
            raise OperationalStatusError(
                "retained Stage-1 authority is absent for a terminal member"
            )
        verify_stage1_wire_member(event, verified)
        if event.surface == Stage1Surface.PLAN.value:
            if verified.required_child_expectations is None:
                raise OperationalStatusError(
                    "plan terminal authority has no required child expectations"
                )
            self.store.reduce_plan_terminal(
                PlanTerminalProjectionRequest(event, verified.required_child_expectations)
            )
            return
        outcome = self.store.reduce(event)
        if outcome.kind is ReductionKind.PARK:
            return
        if (
            verified.owner_generation is None
            or event.parent_flow_id is None
            or event.outcome is None
        ):
            raise OperationalStatusError(
                "unit terminal authority state is incomplete for child reduction"
            )
        self.store.project_child_outcome(
            ChildOutcomeV2(
                parent_flow_id=event.parent_flow_id,
                child_flow_id=event.flow_id,
                child_event_id=event.event_id,
                child_transition_sequence=event.transition_sequence,
                child_execution_id=event.execution_id,
                owner_generation=verified.owner_generation,
                lifecycle_state=event.state,
                completion_outcome=event.outcome,
            )
        )


def _stage1_v2_terminal(event: OperationalDataFlowEvent) -> bool:
    return event.state in TERMINAL_FLOW_STATES and event.surface in {
        Stage1Surface.PLAN.value,
        Stage1Surface.UNIT.value,
    }


def apply_records(event: object, projector: Projector) -> tuple[str, ...]:
    """Return SQS message identifiers only for records that must retry or redrive."""
    if not isinstance(event, dict):
        return ()
    records = cast("dict[str, object]", event).get("Records")
    if not isinstance(records, list):
        return ()
    failures: list[str] = []
    for value in cast("list[object]", records):
        if not isinstance(value, dict):
            continue
        record = cast("dict[str, object]", value)
        try:
            projector.apply(record.get("body"))
        except Exception as error:
            report_error(error, Report(operation=_OPERATION, correlation_id="", logger=_LOGGER))
            identifier = record.get("messageId")
            if isinstance(identifier, str) and identifier:
                failures.append(identifier)
    return tuple(failures)


_PROJECTOR = Projector(
    DynamoOperationalStatusStore(_SETTINGS.status_table),
    DynamoStage1RetainedEventAuthorityReader(_SETTINGS.publications_table),
)


@trace_boundary(OPERATIONAL_STATUS_PROJECTOR)
@flushes_traces
@flushes_metrics
@_LOGGER.inject_lambda_context
def handler(event: dict[str, Any], _context: object) -> dict[str, list[dict[str, str]]]:
    """Project a bounded FIFO batch and expose exact partial failures."""
    with span(OPERATIONAL_STATUS_PROJECTOR, operation=_OPERATION) as process_span:
        records = event.get("Records")
        record_count = len(cast("list[object]", records)) if isinstance(records, list) else 0
        failures = apply_records(event, _PROJECTOR)
        failed_count = len(failures)
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
            _LOGGER,
        )
        return {"batchItemFailures": [{"itemIdentifier": value} for value in failures]}


# S5 (FR-3): receipt rows are written with their measurement contribution, atomically.
_GRAPH_STORE = DynamoGraphProjectionStore(
    _SETTINGS.status_table,
    metrics=DynamoMetricStore(_SETTINGS.status_table, boto3.client("dynamodb")),
)
_STREAM_EVENTS = frozenset({"INSERT", "MODIFY"})


def apply_graph_records(event: object, store: GraphProjectionWriter) -> tuple[str, ...]:
    """Project committed graph items in stream order; return the first failed sequence number.

    A stream shard is ordered, so processing stops at the first failure and reports it; the
    event source retries from there, and every row is an idempotent put, so a replay of the
    records before it writes nothing new.
    """
    if not isinstance(event, dict):
        return ()
    records = cast("dict[str, object]", event).get("Records")
    if not isinstance(records, list):
        return ()
    for value in cast("list[object]", records):
        if not isinstance(value, dict):
            continue
        record = cast("dict[str, Any]", value)
        change = cast("dict[str, Any]", record.get("dynamodb") or {})
        sequence = str(change.get("SequenceNumber", ""))
        if record.get("eventName") not in _STREAM_EVENTS:
            continue
        try:
            image = change.get("NewImage")
            if not isinstance(image, dict):
                raise OperationalStatusError("a graph stream record carries no new image")
            row = project_graph_item(decode_item(cast("dict[str, Any]", image)))
            if row is not None:
                store.apply(row)
        except Exception as error:
            report_error(error, Report(operation=_OPERATION, correlation_id="", logger=_LOGGER))
            return (sequence,) if sequence else ()
    return ()


# The module's one `trace_boundary` root is `handler`; this entry records its batch in the same
# projector span vocabulary without claiming a second boundary owner (no new span in S2, whose
# span set feeds insight cache keys).
@flushes_traces
@flushes_metrics
@_LOGGER.inject_lambda_context
def graph_handler(event: dict[str, Any], _context: object) -> dict[str, list[dict[str, str]]]:
    """Project one graph-table stream batch into status v2 and expose the first failure."""
    with span(OPERATIONAL_STATUS_PROJECTOR, operation=_OPERATION) as process_span:
        records = event.get("Records")
        record_count = len(cast("list[object]", records)) if isinstance(records, list) else 0
        failures = apply_graph_records(event, _GRAPH_STORE)
        outcome = "completed" if not failures else "partial_retry"
        process_span.record(outcome=outcome, record_count=record_count, failed_count=len(failures))
        report_operation(
            OperationLog(
                operation=_OPERATION,
                component=_COMPONENT,
                phase="terminal",
                outcome=outcome,
                severity=LogSeverity.INFO if not failures else LogSeverity.WARNING,
                details={
                    "source": "execution_graph_stream",
                    "record_count": record_count,
                    "failed_count": len(failures),
                },
            ),
            _LOGGER,
        )
        return {"batchItemFailures": [{"itemIdentifier": value} for value in failures]}
