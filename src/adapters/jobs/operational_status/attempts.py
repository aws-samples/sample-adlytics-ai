# SPDX-License-Identifier: MIT-0
"""Semantic-attempt stream producer for authority-backed operational status events."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, cast

import boto3
from aws_lambda_powertools import Logger

from adapters.config import operational_attempts_settings, tracing_settings
from adapters.outbound.dynamodb.items import read_text, write_text
from adapters.outbound.dynamodb.semantic_state import DynamoAttemptStore
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
    OPERATIONAL_STATUS_ATTEMPTS,
    span,
    trace_boundary,
)
from domain.operations.status import OperationalStatusError, map_attempt_history
from domain.semantics.publication import AttemptEvent, AttemptId
from ports.outbound.operational_status import OperationalStatusSink

if TYPE_CHECKING:
    from types_boto3_dynamodb.client import DynamoDBClient

_MAX_ATTEMPT_CHAIN = 32
_SETTINGS = operational_attempts_settings()
_TRACING_SETTINGS = tracing_settings()
_TRACING = configure_tracing(_TRACING_SETTINGS)
_LOGGER = Logger(service="adlytics-operational-status-attempts")
_OPERATION = OPERATIONAL_STATUS_ATTEMPTS
_COMPONENT = "operational_status_attempts"

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
class AttemptTrigger:
    """Exact stream key and immutable payload used only for readback comparison."""

    attempt_id: str
    event_key: str
    payload: str


class AttemptAuthority(Protocol):
    """Consistently read exact attempt history and immutable event payloads."""

    def events_for(self, attempt_id: str) -> tuple[AttemptEvent, ...]:
        """Return the exact stored event history for one attempt identity."""
        ...

    def exact_payload(self, attempt_id: str, event_key: str) -> str | None:
        """Return the exact immutable payload for one event, or None when absent."""
        ...


class DynamoAttemptAuthority:
    """Adapter over the existing attempt ledger with constructor-injected client."""

    def __init__(self, table: str, client: DynamoDBClient | None = None) -> None:
        self._table = table
        self._client = client or boto3.client("dynamodb")
        self._store = DynamoAttemptStore(table, self._client)

    def events_for(self, attempt_id: str) -> tuple[AttemptEvent, ...]:
        """Read the attempt event history through the existing ledger store."""
        return self._store.events_for(AttemptId(attempt_id))

    def exact_payload(self, attempt_id: str, event_key: str) -> str | None:
        """Read one immutable event payload with a consistent point read."""
        response = self._client.get_item(
            TableName=self._table,
            Key={"attempt_id": write_text(attempt_id), "event_key": write_text(event_key)},
            ConsistentRead=True,
        )
        item = response.get("Item")
        return None if item is None else read_text(dict(item), "payload")


@dataclass(frozen=True, slots=True)
class AttemptsProducer:
    """Re-attest one trigger, reconstruct exact predecessor history, then resend."""

    authority: AttemptAuthority
    sink: OperationalStatusSink

    def apply(self, trigger: AttemptTrigger) -> None:
        """Resend the deterministic status prefix ending at the triggering event."""
        exact = self.authority.exact_payload(trigger.attempt_id, trigger.event_key)
        if exact is None or exact != trigger.payload:
            raise OperationalStatusError("attempt stream image failed exact consistent readback")
        newest = self.authority.events_for(trigger.attempt_id)
        target = next((event for event in newest if event.event_key == trigger.event_key), None)
        if target is None:
            raise OperationalStatusError("attempt stream event is absent from exact history")
        chain: list[tuple[AttemptEvent, ...]] = [newest]
        cursor = newest[0] if newest else None
        while cursor is not None and cursor.predecessor is not None:
            if len(chain) >= _MAX_ATTEMPT_CHAIN:
                raise OperationalStatusError("attempt predecessor chain exceeds its bound")
            prior = self.authority.events_for(cursor.predecessor.value)
            if not prior:
                raise OperationalStatusError("attempt predecessor history is unavailable")
            chain.append(prior)
            cursor = prior[0]
        history = tuple(event for group in reversed(chain) for event in group)
        mapped = map_attempt_history(history)
        target_position = next(
            index
            for index, event in enumerate(history)
            if event.attempt_id == target.attempt_id and event.event_key == target.event_key
        )
        for event in mapped[: target_position + 1]:
            self.sink.send(event)


def _new_image(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    record = cast("dict[str, object]", value)
    if record.get("eventName") not in {"INSERT", "MODIFY"}:
        return None
    dynamodb = record.get("dynamodb")
    if not isinstance(dynamodb, dict):
        return None
    image = cast("dict[str, object]", dynamodb).get("NewImage")
    return cast("dict[str, object]", image) if isinstance(image, dict) else None


def parse_record(value: object) -> AttemptTrigger | None:
    """Accept only INSERT/MODIFY NEW_IMAGE records with the exact attempt key shape."""
    attributes = _new_image(value)
    if attributes is None:
        return None
    values: list[str] = []
    for name in ("attempt_id", "event_key", "payload"):
        attribute = attributes.get(name)
        if not isinstance(attribute, dict):
            return None
        text = cast("dict[str, object]", attribute).get("S")
        if not isinstance(text, str) or not text:
            return None
        values.append(text)
    return AttemptTrigger(*values)


def apply_records(event: object, producer: AttemptsProducer) -> tuple[str, ...]:
    """Return DynamoDB SequenceNumbers only for valid records that need retry."""
    if not isinstance(event, dict):
        return ()
    records = cast("dict[str, object]", event).get("Records")
    if not isinstance(records, list):
        return ()
    failures: list[str] = []
    for value in cast("list[object]", records):
        trigger = parse_record(value)
        if trigger is None:
            continue
        try:
            producer.apply(trigger)
        except Exception as error:
            report_error(error, Report(operation=_OPERATION, correlation_id="", logger=_LOGGER))
            sequence = _sequence_number(value)
            if sequence is not None:
                failures.append(sequence)
    return tuple(failures)


def _sequence_number(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    dynamodb = cast("dict[str, object]", value).get("dynamodb")
    if not isinstance(dynamodb, dict):
        return None
    sequence = cast("dict[str, object]", dynamodb).get("SequenceNumber")
    return sequence if isinstance(sequence, str) and sequence else None


_PRODUCER = AttemptsProducer(
    DynamoAttemptAuthority(_SETTINGS.attempts_table),
    SqsOperationalStatusSink(_SETTINGS.queue_url),
)


@trace_boundary(OPERATIONAL_STATUS_ATTEMPTS)
@flushes_traces
@flushes_metrics
@_LOGGER.inject_lambda_context
def handler(event: dict[str, Any], _context: object) -> dict[str, list[dict[str, str]]]:
    """Produce deterministic direct-FIFO status events with partial stream retry."""
    with span(OPERATIONAL_STATUS_ATTEMPTS, operation=_OPERATION) as process_span:
        records = event.get("Records")
        record_count = len(cast("list[object]", records)) if isinstance(records, list) else 0
        failures = apply_records(event, _PRODUCER)
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
